"""チャンネルを監視し、配信が始まったら録画とチャット記録を行い、自動でショート動画を作る。

1 回の配信 = 1 つの「セッション」(work/<チャンネル>/<日時>/):
  - 録画 (stream.ts。録画が途切れても配信が続いていれば stream_2.ts … と録り直す)
  - チャット (chat.jsonl)・同時視聴者数 (viewers.jsonl)。時刻はどちらも録画開始からの経過秒
  - parts.json: 各録画ファイルが録画開始から何秒目に始まったか
配信が終わったら、その配信の VOD から正式版を作る (VOD が無ければ録画から)。
"""

from __future__ import annotations

import json
import logging
import shutil
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .chat import LiveChatRecorder, load_chat
from .config import Config
from .fileutil import write_json_atomic
from .download import is_live_via_ytdlp, start_live_recording
from .models import ChatMessage, Highlight, ViewerSample
from .pipeline import LocalSource, process
from .viewers import ViewerRecorder, load_viewers

log = logging.getLogger(__name__)

# 録画中ファイルの末尾付近は書き込み途中なので、この秒数ぶん手前までを処理対象にする
LIVE_TAIL_MARGIN = 45.0
# 録画が途切れたときに録り直す最大回数と、配信が続いているか確かめる回数・間隔
MAX_RECORDER_RESTARTS = 5
RESTART_BACKOFF_SECONDS = 10.0
MAX_LEFTOVER_ATTEMPTS = 2


class LiveChecker:
    def __init__(self, cfg: Config):
        self.helix = None
        if cfg.twitch.client_id and cfg.twitch.client_secret:
            from .twitch_api import HelixClient

            self.helix = HelixClient(cfg.twitch.client_id, cfg.twitch.client_secret)

    def is_live(self, channel: str) -> bool:
        try:
            if self.helix:
                return self.helix.is_live(channel)
            return is_live_via_ytdlp(channel)
        except Exception as e:
            log.warning("配信状態の確認に失敗しました: %s", e)
            return False


# ---------------------------------------------------------------------------
# 停止の扱い
# ---------------------------------------------------------------------------

# 停止の指示 (SIGTERM など) を受けたか
_STOP = threading.Event()
# 本体 (メインスレッド) が書き出しなどの処理中か。処理中に停止の指示を受けたら、終わってから止まる
_MAIN_BUSY = threading.Event()
# 前回の残りを処理する裏のスレッド
_BACKGROUND: list[threading.Thread] = []


@contextmanager
def _busy():
    """この間は停止の指示を受けても処理を中断しない (書き出し途中で止まると成果物が残らないため)。"""
    nested = _MAIN_BUSY.is_set()
    _MAIN_BUSY.set()
    try:
        yield
    finally:
        if not nested:
            _MAIN_BUSY.clear()


def _stop_on_terminate() -> None:
    """サービスの停止や PC のシャットダウン (SIGTERM / Windows の Ctrl+Break) を受けたら止まる。

    録画中・待機中なら Ctrl+C と同じく、ここまでの録画を処理して終了する。
    本体が処理中なら、その処理が終わってから終了する。
    """
    def handler(signum, frame):
        _STOP.set()
        if _MAIN_BUSY.is_set():
            log.info("停止の指示を受けました。いまの処理が終わったら終了します")
            return
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # メインスレッド以外など
                pass


def _sleep_checking_stop(seconds: float) -> None:
    """待っている間に停止の指示があれば止まる。"""
    deadline = time.time() + seconds
    while True:
        if _STOP.is_set():
            raise KeyboardInterrupt
        left = deadline - time.time()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def _wait_background() -> None:
    """終了する前に、裏で処理中の前回の残りが終わるのを待つ (途中で止めると無駄になるため)。"""
    for t in _BACKGROUND:
        if t.is_alive():
            log.info("前回の残りの処理が終わるのを待っています…")
            t.join()


def wait_until_live(cfg: Config, channel: str, checker: LiveChecker | None = None) -> None:
    checker = checker or LiveChecker(cfg)
    logged = False
    while not checker.is_live(channel):
        if not logged:
            log.info("%s の配信開始を待っています (%d 秒ごとに確認)", channel, cfg.watch.poll_interval)
            logged = True
        _sleep_checking_stop(cfg.watch.poll_interval)


# ---------------------------------------------------------------------------
# セッションのデータ
# ---------------------------------------------------------------------------

def _mark(session_dir: Path, name: str) -> None:
    try:
        (session_dir / name).write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except OSError as e:
        log.error("処理済みの印を書けませんでした (%s)。次回の起動時にもう一度処理される可能性があります: %s",
                  session_dir / name, e)


def _save_meta(session_dir: Path, **values) -> None:
    """セッションの情報 (録画開始時刻・配信開始時刻・使った VOD) を session.json に足す。"""
    meta = _load_meta(session_dir)
    meta.update(values)
    write_json_atomic(session_dir / "session.json", meta)


def _load_meta(session_dir: Path) -> dict:
    try:
        meta = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_parts(session_dir: Path, parts: list[tuple[Path, float]]) -> None:
    write_json_atomic(session_dir / "parts.json", [{"file": p.name, "offset": round(o, 2)} for p, o in parts])


def _load_parts(session_dir: Path) -> list[tuple[Path, float]]:
    """録画ファイルと、それが録画開始から何秒目に始まったか。中身の無いファイルは除く。"""
    parts = [(session_dir / "stream.ts", 0.0)]
    try:
        data = json.loads((session_dir / "parts.json").read_text(encoding="utf-8"))
        parts = [(session_dir / d["file"], float(d["offset"])) for d in data]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return [(p, o) for p, o in parts if p.exists() and p.stat().st_size > 0]


def _shift_signals(chat: list[ChatMessage], viewers: list[ViewerSample] | None,
                   shift: float) -> tuple[list[ChatMessage], list[ViewerSample] | None]:
    if not shift:
        return chat, viewers
    return ([replace(m, offset=m.offset + shift) for m in chat],
            [replace(v, offset=v.offset + shift) for v in viewers] if viewers else None)


def _load_signals(session_dir: Path, shift: float = 0.0) -> tuple[list[ChatMessage], list[ViewerSample] | None]:
    """セッションのチャットと視聴者数を、時刻を shift 秒ずらして読む。"""
    chat_path, viewers_path = session_dir / "chat.jsonl", session_dir / "viewers.jsonl"
    chat: list[ChatMessage] = []
    viewers: list[ViewerSample] | None = None
    try:
        if chat_path.exists() and chat_path.stat().st_size:
            chat = load_chat(chat_path)
    except (OSError, ValueError) as e:
        log.warning("チャットの記録を読めませんでした (チャット無しで続けます): %s", e)
    try:
        if viewers_path.exists() and viewers_path.stat().st_size:
            viewers = load_viewers(viewers_path) or None
    except (OSError, ValueError) as e:
        log.warning("視聴者数の記録を読めませんでした: %s", e)
    return _shift_signals(chat, viewers, shift)


# ---------------------------------------------------------------------------
# 録画
# ---------------------------------------------------------------------------

def record_and_process(cfg: Config, channel: str, helix=None, dry_run: bool = False,
                       checker: LiveChecker | None = None) -> list[Highlight]:
    """1 回分の配信を録画し、ショート動画を作る。配信が終わったら戻る。

    ``helix`` (HelixClient) があれば同時視聴者数も記録し、検出のシグナルに使う。
    ``dry_run`` なら検出だけ行い、動画の書き出しと Twitch クリップの作成はしない。
    ``checker`` があれば、録画が途切れたときの配信状態の確認に使う (API の認証情報が無くても確認できる)。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path(cfg.work_dir) / channel / stamp
    session_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(cfg.output_dir) / channel / stamp
    chat_path = session_dir / "chat.jsonl"

    parts = [(session_dir / "stream.ts", 0.0)]
    _save_parts(session_dir, parts)
    proc = start_live_recording(channel, parts[0][0], cfg.watch.recorder, cfg.watch.quality)
    start_time = time.time()
    chat_rec = LiveChatRecorder(channel, chat_path, start_time, cfg.twitch.irc_oauth_token, cfg.twitch.irc_nick)
    chat_rec.start()
    viewer_rec = None
    previews: dict[str, list[Highlight]] = {}  # 録画ファイルごとのプレビュー (時刻の基準がファイルごとに違うため)
    interrupted = False
    stream_started = None
    next_run = start_time + cfg.watch.rolling_minutes * 60
    try:
        _save_meta(session_dir, start_time=start_time)
        # チャットの記録を始めてから問い合わせる (API が遅くても配信冒頭のチャットを取りこぼさない)
        stream_started = _stream_started_at(helix, channel)
        _save_meta(session_dir, stream_started=stream_started)
        if helix is not None:
            viewer_rec = ViewerRecorder(helix, channel, session_dir / "viewers.jsonl", start_time,
                                        cfg.watch.viewer_poll_interval)
            viewer_rec.start()
        else:
            log.info("Twitch API の認証情報が無いため、同時視聴者数は記録しません")
        log.info("録画中: %s / チャット: %s", parts[0][0], chat_path)
        quick_failures = 0
        while True:
            part_started = time.time()
            while proc.poll() is None:
                time.sleep(5)
                if _STOP.is_set():
                    raise KeyboardInterrupt
                if cfg.watch.rolling_minutes > 0 and time.time() >= next_run:
                    next_run = time.time() + cfg.watch.rolling_minutes * 60
                    done = previews.setdefault(parts[-1][0].name, [])
                    done += _preview(cfg, channel, session_dir, parts[-1], run_dir, done,
                                     time.time() - start_time, dry_run)
            # 録画が終わった: 配信が終わったのか、録画だけが途切れたのか確かめる
            if _STOP.is_set():
                raise KeyboardInterrupt
            if not _still_live(helix, channel, checker):
                break
            # すぐに終わってしまう録画が続く場合 (配信終了直後で API の反映が遅れている場合も含む) は、
            # 録画をあきらめて配信の終了を待つ (VOD から作るので取りこぼさない)
            quick_failures = quick_failures + 1 if time.time() - part_started < 60 else 0
            if quick_failures >= MAX_RECORDER_RESTARTS:
                log.warning("録画を再開できません。配信の終了を待ってから作ります")
                _wait_until_offline(cfg, helix, channel, checker)
                break
            if quick_failures:
                _sleep_checking_stop(RESTART_BACKOFF_SECONDS)
            part = session_dir / f"stream_{len(parts) + 1}.ts"
            log.warning("録画が途切れましたが配信は続いています。録り直します (%s)", part.name)
            try:
                proc = start_live_recording(channel, part, cfg.watch.recorder, cfg.watch.quality)
            except Exception as e:  # 録画ツールが起動できない等: ここまでの分を処理する
                log.error("録画を再開できませんでした。ここまでの分を処理します: %s", e)
                break
            parts.append((part, time.time() - start_time))
            try:
                _save_parts(session_dir, parts)
            except OSError as e:
                log.error("録画ファイルの一覧を保存できませんでした: %s", e)
    except KeyboardInterrupt:
        log.info("中断されました。録画を止めて、ここまでの分を処理してから終了します")
        proc.terminate()
        interrupted = True
    finally:
        # ここから最終処理が終わるまでは、停止の指示を受けても中断しない (後片付けの途中で止まると最終処理が飛ぶため)
        _MAIN_BUSY.set()
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
        chat_rec.stop()
        if viewer_rec:
            viewer_rec.stop()

    try:
        if proc.returncode not in (0, None, -15) and proc.stderr:
            log.warning("録画プロセスの出力: %s", proc.stderr.read().decode(errors="replace")[-500:])
        if not _load_parts(session_dir):
            log.error("録画ファイルがありません。チャンネル名や録画ツールを確認してください")
            if interrupted:
                raise KeyboardInterrupt
            return [h for hs in previews.values() for h in hs]
        log.info("配信終了。チャット %d 件。最終処理を行います", chat_rec.count)
        result = _final_pass(cfg, channel, helix, start_time, stream_started, session_dir, run_dir, dry_run) or []
    finally:
        _MAIN_BUSY.clear()
    if interrupted or _STOP.is_set():
        # Ctrl+C / 停止の指示は「止める」意味なので、配信が続いていても次の録画は始めずに終了する
        raise KeyboardInterrupt
    return result


def _wait_until_offline(cfg: Config, helix, channel: str, checker: LiveChecker | None = None) -> None:
    while _still_live(helix, channel, checker):
        _sleep_checking_stop(cfg.watch.poll_interval)


def _still_live(helix, channel: str, checker: LiveChecker | None = None) -> bool:
    """配信が続いているか (確認できなければ終わったとみなす)。

    配信終了の直後は API がしばらく「配信中」と返すことがあるが、その場合は録り直しがすぐに失敗するので、
    呼び出し側で「すぐ終わる録画が続いたら配信の終了を待つ」ことで対処する。
    """
    if checker is not None:
        return checker.is_live(channel)
    if helix is None:
        return False
    try:
        return bool(helix.is_live(channel))
    except Exception:
        return False


def _preview(cfg: Config, channel: str, session_dir: Path, part: tuple[Path, float], run_dir: Path,
             previews: list[Highlight], elapsed: float, dry_run: bool) -> list[Highlight]:
    """配信中のプレビュー (live/ に保存。投稿予定表・Twitch クリップは配信後の正式版だけ)。"""
    video, offset = part
    if not video.exists():
        return []
    chat, viewers = _load_signals(session_dir, -offset)
    with _busy():
        result = _process_recording(cfg, channel, video, chat, viewers, run_dir / "live" / video.stem, previews,
                                    elapsed - offset - LIVE_TAIL_MARGIN, dry_run, publish=False)
    return result or []


# ---------------------------------------------------------------------------
# 最終処理
# ---------------------------------------------------------------------------

def _final_pass(cfg: Config, channel: str, helix, start_time: float, stream_started: float | None,
                session_dir: Path, run_dir: Path, dry_run: bool, leftover: bool = False) -> list[Highlight] | None:
    """配信終了後の最終処理 (正式版)。

    この配信の VOD が見つかれば VOD から作る: VOD には広告が入らず時刻も正確なので、ショートの切り出し位置と
    Twitch クリップの位置が合う。見つからない・作れない場合は録画から作る。
    最後まで処理できたときだけ "processed" の印を付ける (失敗したら None を返し、次回の起動時にやり直す)。
    ``leftover`` (前回の残りの処理) のときは印を付けない (呼び出し側が付ける)。また、いまの配信の開始時刻を
    この録画の配信の開始時刻と取り違えないよう、配信の開始時刻を問い合わせない。
    """
    vod = None
    if helix is not None:
        try:
            started = stream_started
            if started is None and not leftover:
                started = _stream_started_at(helix, channel)
            # 録画は配信より少し遅れて届くので、その分だけ VOD 上では前の位置になる
            info = find_stream_vod_info(helix, channel, start_time - cfg.watch.stream_latency, started)
            if info:
                vod_id, shift, vod_duration = info
                session_length = _session_length(session_dir)
                if shift + session_length > vod_duration + 120:
                    # 配信が途中で切れて別の VOD に分かれた場合など。この VOD だけでは録画の全体を作れない
                    log.warning("VOD (%s) が録画の途中で終わっているため、録画から作ります", vod_id)
                else:
                    vod = (vod_id, shift, vod_duration)
        except Exception as e:
            log.warning("この配信の VOD を確認できませんでした: %s", e)
        if vod is None:
            log.warning("この配信の VOD が見つからないため、録画から作ります (Twitch クリップは作りません。"
                        "Twitch の設定で「過去の配信を保存」を有効にすると作れます)")
    result = None
    if vod and _vod_in_latest_state(cfg, vod[0]):
        log.info("この配信の VOD (%s) は latest コマンドで作成済みです", vod[0])
        result = []
    elif vod:
        result = _run_from_vod(cfg, channel, vod, session_dir, run_dir, dry_run,
                               _highlights_of_vod(cfg, channel, vod[0], session_dir))
        if result is not None and not dry_run:
            try:
                _save_meta(session_dir, vod_id=vod[0])  # 同じ VOD を別のセッションで二重に作らないための記録
            except OSError as e:
                log.error("使った VOD を記録できませんでした (%s): %s", session_dir, e)
    if result is None:
        result = _run_from_recording(cfg, channel, session_dir, run_dir, dry_run)
    if result is not None and not leftover:
        # dry-run の録画は試しに検出しただけなので、後で正式に処理し直さないよう印を分ける
        _mark(session_dir, "dry_run" if dry_run else "processed")
    return result


def _vod_in_latest_state(cfg: Config, vod_id: str) -> bool:
    """latest コマンドがこの VOD をすでに処理したか。"""
    try:
        return vod_id in json.loads((Path(cfg.work_dir) / "processed_vods.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False


def _highlights_of_vod(cfg: Config, channel: str, vod_id: str, me: Path) -> list[Highlight]:
    """同じ VOD から別のセッションで作ったショート (配信中に監視を再起動した場合など)。重ねて作らないために使う。"""
    found: list[Highlight] = []
    base = Path(cfg.work_dir) / channel
    for other in sorted(base.glob("*/")) if base.exists() else []:
        if other == me or _load_meta(other).get("vod_id") != vod_id:
            continue
        try:
            data = json.loads((Path(cfg.output_dir) / channel / other.name / "highlights.json")
                              .read_text(encoding="utf-8"))
            found += [Highlight.from_dict(d) for d in data.get("highlights", []) if d.get("output_path")]
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return found


def _run_from_vod(cfg: Config, channel: str, vod: tuple[str, float, float], session_dir: Path, run_dir: Path,
                  dry_run: bool, exclude: list[Highlight] | None = None) -> list[Highlight] | None:
    """録画の時刻で記録したチャット・視聴者数を VOD の時刻に直し、VOD から作る。

    VOD を取得できなかった (1 本も書き出せなかった) ら None を返し、録画から作り直してもらう。
    """
    from .download import vod_url
    from .pipeline import VodSource

    vod_id, shift, vod_duration = vod
    chat, viewers = _load_signals(session_dir, shift)
    # セッション専用の作業フォルダー (他のコマンドが使う work/vod_<id> の中身は消さない)
    work = Path(cfg.work_dir) / f"vod_{vod_id}" / f"auto_{session_dir.name}"
    log.info("この配信の VOD (%s) から作ります", vod_id)
    try:
        result = process(cfg, VodSource(vod_url(vod_id), work, cfg.watch.quality), chat, run_dir,
                         duration=vod_duration, channel=channel, viewers=viewers, clip_vod=(vod_id, 0.0),
                         dry_run=dry_run, exclude=exclude)
    except Exception as e:
        log.warning("VOD から作れなかったため、録画から作ります: %s", e)
        return None
    finally:
        # ダウンロードした音声・区間は書き出し後は不要 (残すとディスクを圧迫する)
        shutil.rmtree(work, ignore_errors=True)
    if not dry_run and result.highlights and not any(h.output_path for h in result.highlights):
        log.warning("VOD を取得できなかったため、録画から作ります")
        return None
    for h in result.highlights:
        log.info("作成: %s  %s", h.output_path, h.title)
    return result.highlights


def _run_from_recording(cfg: Config, channel: str, session_dir: Path, run_dir: Path,
                        dry_run: bool) -> list[Highlight] | None:
    """録画ファイルから作る (録り直した分は part2/ … に分けて保存)。

    配信終了の間際に録り直してすぐ終わったような短いファイルが失敗しても、他が作れていれば成功とする。
    1 つも作れなかったら None。
    """
    chat, viewers = _load_signals(session_dir)
    done: list[Highlight] = []
    ok = False
    for i, (video, offset) in enumerate(_load_parts(session_dir)):
        part_chat, part_viewers = _shift_signals(chat, viewers, -offset)
        out = run_dir if i == 0 else run_dir / f"part{i + 1}"
        result = _process_recording(cfg, channel, video, part_chat, part_viewers, out, [], None, dry_run)
        if result is None:
            log.warning("録画ファイル %s からは作れませんでした", video.name)
            continue
        ok = True
        done += result
    return done if ok else None


def _process_recording(cfg: Config, channel: str, video: Path, chat: list[ChatMessage],
                       viewers: list[ViewerSample] | None, run_dir: Path, exclude: list[Highlight],
                       available_until: float | None, dry_run: bool, publish: bool = True) -> list[Highlight] | None:
    """録画ファイル 1 本から作る。失敗したら None。"""
    try:
        result = process(cfg, LocalSource(str(video)), chat, run_dir, channel=channel, exclude=exclude,
                         available_until=available_until, viewers=viewers, dry_run=dry_run, publish=publish)
    except Exception as e:
        log.warning("処理に失敗しました: %s", e)
        return None
    if not dry_run and result.highlights and not any(h.output_path for h in result.highlights):
        log.warning("ショートを 1 本も書き出せませんでした (ディスクの空きや録画ファイルを確認してください)")
        return None
    for h in result.highlights:
        log.info("作成: %s  %s", h.output_path, h.title)
    return result.highlights


def _stream_started_at(helix, channel: str) -> float | None:
    """配信の開始時刻 (VOD の 0 秒に当たる)。取れなければ None。"""
    if helix is None:
        return None
    try:
        stream = helix.get_stream(channel)
        if stream and stream.get("started_at"):
            from .twitch_api import parse_rfc3339

            return parse_rfc3339(stream["started_at"]).timestamp()
    except Exception as e:
        log.debug("配信の開始時刻を取得できませんでした: %s", e)
    return None


def _session_length(session_dir: Path) -> float:
    """録画開始から最後の録画ファイルの終わりまでの秒数 (おおよそ)。"""
    from .audio import probe_duration

    parts = _load_parts(session_dir)
    if not parts:
        return 0.0
    video, offset = parts[-1]
    try:
        return offset + probe_duration(str(video))
    except Exception:
        return offset


def find_stream_vod_info(helix, channel: str, record_start: float, stream_started: float | None = None,
                         user_id: str | None = None) -> tuple[str, float, float] | None:
    """録画中の配信の VOD と、録画の 0 秒が VOD の何秒目か (= 録画開始の遅れ) を返す。

    stream_started: 配信の開始時刻 (Get Streams の started_at)。分かればそれと VOD の開始が一致するものを選ぶ。
    分からなければ「録画開始より前に始まり、録画開始の時点より後まで続いている」VOD を選ぶ
    (直前に終わった前回の配信の VOD は録画開始の時点で終わっているので選ばれない)。
    """
    user_id = user_id or helix.get_user_id(channel)
    for v in helix.get_recent_archives(user_id, 3):
        started = v.created_at.timestamp()
        if stream_started is not None:
            same = abs(started - stream_started) <= 600
        else:
            same = started <= record_start + 120 and started + v.duration > record_start + 60
        if same:
            return v.id, max(0.0, record_start - started), float(v.duration)
    return None


# ---------------------------------------------------------------------------
# 前回の残り
# ---------------------------------------------------------------------------

def process_leftovers(cfg: Config, channel: str, dry_run: bool = False, checker: LiveChecker | None = None) -> None:
    """前回、最後まで処理する前に止まってしまった録画 (停電・強制終了・処理の失敗など) を処理する。

    - 配信中は処理しない (同じ PC の配信を重くしないため)。途中で配信が始まったら、処理中の 1 件を
      終えたところで中断する
    - 通常の最終処理と同じく、VOD があれば VOD から作る
    - 同じ VOD を別のセッションがすでに処理していれば、その録画は処理済みとする (二重に作らない)
    - 対象は "processed" の印が無い録画。壊れた録画で毎回失敗し続けないよう、失敗は 2 回まで
    """
    helix = checker.helix if checker else None
    base = Path(cfg.work_dir) / channel
    sessions = sorted(base.glob("*/")) if base.exists() else []
    for session_dir in sessions:
        if _STOP.is_set():
            return
        run_dir = Path(cfg.output_dir) / channel / session_dir.name
        # parts.json が無いのは以前の版で作った録画 (その版で処理済み) なので対象にしない
        if (session_dir / "processed").exists() or (session_dir / "dry_run").exists() \
                or not (session_dir / "parts.json").exists() or not _load_parts(session_dir):
            continue
        attempts_file = session_dir / "attempts"
        try:
            attempts = int(attempts_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            attempts = 0
        if attempts >= MAX_LEFTOVER_ATTEMPTS:
            log.info("処理に %d 回失敗した録画は飛ばします: %s", attempts, session_dir)
            continue
        if checker is not None and checker.is_live(channel):
            return
        meta = _load_meta(session_dir)
        if helix is not None and meta.get("start_time") and _vod_already_done(cfg, helix, channel, meta, sessions,
                                                                             session_dir):
            log.info("この録画の配信は別のセッションで作成済みです: %s", session_dir)
            if not dry_run:
                _mark(session_dir, "processed")
            continue
        log.info("前回処理されなかった録画を処理します: %s", session_dir)
        # (裏のスレッドで動くので停止の合図は受けない。本体は終了前にこの処理の完了を待つ)
        if meta.get("start_time"):
            result = _final_pass(cfg, channel, helix, float(meta["start_time"]), meta.get("stream_started"),
                                 session_dir, run_dir, dry_run, leftover=True)
        else:
            result = _run_from_recording(cfg, channel, session_dir, run_dir, dry_run)
        if dry_run:
            continue
        if result is not None:
            _mark(session_dir, "processed")
        else:
            # 失敗したときだけ数える (途中で止められた分は数えない)
            try:
                attempts_file.write_text(str(attempts + 1), encoding="utf-8")
            except OSError as e:
                log.error("失敗の回数を記録できませんでした (%s): %s", attempts_file, e)


def _vod_already_done(cfg: Config, helix, channel: str, meta: dict, sessions: list[Path], me: Path) -> bool:
    try:
        info = find_stream_vod_info(helix, channel, float(meta["start_time"]) - cfg.watch.stream_latency,
                                    meta.get("stream_started"))
    except Exception:
        return False
    if not info:
        return False
    return any(other != me and _load_meta(other).get("vod_id") == info[0] for other in sessions)


def watch(cfg: Config, channel: str, once: bool = False, dry_run: bool = False) -> None:
    _STOP.clear()
    _stop_on_terminate()
    checker = LiveChecker(cfg)

    def start_leftovers() -> None:
        # 前回の残りは裏で処理する (配信中は処理せず、配信が始まったら止まる)
        if any(t.is_alive() for t in _BACKGROUND):
            return
        t = threading.Thread(target=process_leftovers, args=(cfg, channel, dry_run, checker), daemon=True,
                             name="leftovers")
        _BACKGROUND.append(t)
        t.start()

    try:
        while True:
            start_leftovers()
            wait_until_live(cfg, channel, checker)
            log.info("%s が配信を開始しました", channel)
            record_and_process(cfg, channel, checker.helix, dry_run=dry_run, checker=checker)
            if once:
                return
            # 配信終了直後の再接続などで即座に再録画しないよう少し待つ
            _sleep_checking_stop(cfg.watch.poll_interval)
    finally:
        # 裏の処理には「いまの録画が終わったら次へ進まない」よう伝えてから、終わるのを待つ
        _STOP.set()
        _wait_background()
