"""チャンネルを監視し、配信が始まったら録画とチャット記録を行い、自動でショート動画を作る。"""

from __future__ import annotations

import logging
import signal
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .chat import LiveChatRecorder, load_chat
from .config import Config
from .download import is_live_via_ytdlp, start_live_recording
from .models import Highlight
from .pipeline import LocalSource, process
from .viewers import ViewerRecorder, load_viewers

log = logging.getLogger(__name__)

# 録画中ファイルの末尾付近は書き込み途中なので、この秒数ぶん手前までを処理対象にする
LIVE_TAIL_MARGIN = 45.0


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


def wait_until_live(cfg: Config, channel: str, checker: LiveChecker | None = None) -> None:
    checker = checker or LiveChecker(cfg)
    logged = False
    while not checker.is_live(channel):
        if not logged:
            log.info("%s の配信開始を待っています (%d 秒ごとに確認)", channel, cfg.watch.poll_interval)
            logged = True
        _sleep_checking_stop(cfg.watch.poll_interval)


def _sleep_checking_stop(seconds: float) -> None:
    """待っている間に停止の指示があり、裏の処理も終わっていれば止まる。"""
    deadline = time.time() + seconds
    while True:
        if _STOP.is_set() and not _busy_count():
            raise KeyboardInterrupt
        left = deadline - time.time()
        if left <= 0:
            return
        time.sleep(min(1.0, left))


def record_and_process(cfg: Config, channel: str, helix=None, dry_run: bool = False) -> list[Highlight]:
    """1 回分の配信を録画し、ショート動画を作る。配信が終わったら戻る。

    ``helix`` (HelixClient) があれば同時視聴者数も記録し、検出のシグナルに使う。
    ``dry_run`` なら検出だけ行い、動画の書き出しと Twitch クリップの作成はしない。
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_dir = Path(cfg.work_dir) / channel / stamp
    session_dir.mkdir(parents=True, exist_ok=True)
    run_dir = Path(cfg.output_dir) / channel / stamp
    video_path = session_dir / "stream.ts"
    chat_path = session_dir / "chat.jsonl"
    viewers_path = session_dir / "viewers.jsonl"

    proc = start_live_recording(channel, video_path, cfg.watch.recorder, cfg.watch.quality)
    start_time = time.time()
    chat_rec = LiveChatRecorder(channel, chat_path, start_time, cfg.twitch.irc_oauth_token, cfg.twitch.irc_nick)
    chat_rec.start()
    # チャットの記録を始めてから問い合わせる (API が遅くても配信冒頭のチャットを取りこぼさない)
    stream_started = _stream_started_at(helix, channel)
    viewer_rec = None
    if helix is not None:
        viewer_rec = ViewerRecorder(helix, channel, viewers_path, start_time, cfg.watch.viewer_poll_interval)
        viewer_rec.start()
    else:
        log.info("Twitch API の認証情報が無いため、同時視聴者数は記録しません")
    log.info("録画中: %s / チャット: %s", video_path, chat_path)

    done: list[Highlight] = []
    interrupted = False
    next_run = start_time + cfg.watch.rolling_minutes * 60
    try:
        while proc.poll() is None:
            time.sleep(5)
            if cfg.watch.rolling_minutes > 0 and time.time() >= next_run and video_path.exists():
                next_run = time.time() + cfg.watch.rolling_minutes * 60
                elapsed = time.time() - start_time
                # 配信中は録画からプレビューを作る (live/ に保存。投稿予定表と Twitch クリップは、
                # 広告が入らず時刻も正確な配信後の VOD から作る正式版だけ)
                with _busy():
                    done += _run(cfg, channel, video_path, chat_path, viewers_path, run_dir / "live", done,
                                 elapsed - LIVE_TAIL_MARGIN, None, dry_run, publish=False) or []
                if _STOP.is_set():
                    raise KeyboardInterrupt
    except KeyboardInterrupt:
        log.info("中断されました。録画を止めて、ここまでの分を処理してから終了します")
        proc.terminate()
        interrupted = True
    finally:
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()
        chat_rec.stop()
        if viewer_rec:
            viewer_rec.stop()

    if proc.returncode not in (0, None, -15) and proc.stderr:
        log.warning("録画プロセスの出力: %s", proc.stderr.read().decode(errors="replace")[-500:])
    if not video_path.exists() or video_path.stat().st_size == 0:
        log.error("録画ファイルがありません。チャンネル名や録画ツールを確認してください")
        if interrupted:
            raise KeyboardInterrupt
        return done
    if not interrupted and helix is not None and _still_live(helix, channel):
        # 録画だけが途切れた (配信は続いている)。次の録画の最終処理で VOD 全体から作るので、ここでは作らない
        log.warning("録画が途切れましたが配信は続いています。配信が終わったあとにまとめて作ります")
        _mark(session_dir, "continued")
        return done
    log.info("配信終了。チャット %d 件。最終処理を行います", chat_rec.count)
    with _busy():
        done = _final_pass(cfg, channel, helix, start_time, stream_started, session_dir, run_dir, dry_run)
    if interrupted or _STOP.is_set():
        # Ctrl+C / 停止の指示は「止める」意味なので、配信が続いていても次の録画は始めずに終了する
        raise KeyboardInterrupt
    return done


def _still_live(helix, channel: str) -> bool:
    try:
        return bool(helix.is_live(channel))
    except Exception:
        return False


def _final_pass(cfg: Config, channel: str, helix, start_time: float, stream_started: float | None,
                session_dir: Path, run_dir: Path, dry_run: bool) -> list[Highlight]:
    """配信終了後の最終処理 (正式版)。

    この配信の VOD が見つかれば VOD から作る: VOD には広告が入らず時刻も正確なので、ショートの切り出し位置と
    Twitch クリップの位置が合う。見つからなければ (VOD を保存しない設定など) 録画から作る。
    最後まで処理できたときだけ "processed" の印を付ける (失敗したら次回の起動時にやり直す)。
    """
    video, chat_path, viewers_path = session_dir / "stream.ts", session_dir / "chat.jsonl", session_dir / "viewers.jsonl"
    vod = None
    if helix is not None:
        try:
            started = stream_started if stream_started is not None else _stream_started_at(helix, channel)
            # 録画は配信より少し遅れて届くので、その分だけ VOD 上では前の位置になる
            vod = find_stream_vod(helix, channel, start_time - cfg.watch.stream_latency, started)
        except Exception as e:
            log.warning("この配信の VOD を確認できませんでした: %s", e)
        if vod is None:
            log.warning("この配信の VOD が見つからないため、録画から作ります (Twitch クリップは作りません。"
                        "Twitch の設定で「過去の配信を保存」を有効にすると作れます)")
    result = _run_from_vod(cfg, channel, vod, chat_path, viewers_path, run_dir, dry_run) if vod else None
    if result is None:
        result = _run(cfg, channel, video, chat_path, viewers_path, run_dir, [], None, None, dry_run)
    if result is not None and not dry_run:
        _mark(session_dir, "processed")
    return result or []


def _run_from_vod(cfg: Config, channel: str, vod: tuple[str, float], chat_path: Path, viewers_path: Path,
                  run_dir: Path, dry_run: bool) -> list[Highlight] | None:
    """録画の時刻で記録したチャット・視聴者数を VOD の時刻に直し、VOD から作る。失敗したら None。"""
    import shutil
    from dataclasses import replace

    from .download import vod_url
    from .pipeline import VodSource

    vod_id, shift = vod
    chat = load_chat(chat_path) if chat_path.exists() and chat_path.stat().st_size else []
    viewers = load_viewers(viewers_path) if viewers_path.exists() and viewers_path.stat().st_size else None
    chat = [replace(m, offset=m.offset + shift) for m in chat]
    viewers = [replace(v, offset=v.offset + shift) for v in viewers] if viewers else None
    work = Path(cfg.work_dir) / f"vod_{vod_id}"
    # 配信中に途中までダウンロードした音声などが残っていると VOD の後半が欠けるので、作り直す
    shutil.rmtree(work, ignore_errors=True)
    log.info("この配信の VOD (%s) から作ります", vod_id)
    try:
        result = process(cfg, VodSource(vod_url(vod_id), work, cfg.watch.quality), chat, run_dir,
                         channel=channel, viewers=viewers, clip_vod=(vod_id, 0.0), dry_run=dry_run)
    except Exception as e:
        log.warning("VOD から作れなかったため、録画から作ります: %s", e)
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


def find_stream_vod(helix, channel: str, record_start: float, stream_started: float | None = None,
                    user_id: str | None = None) -> tuple[str, float] | None:
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
            return v.id, max(0.0, record_start - started)
    return None


def _run(cfg: Config, channel: str, video: Path, chat_path: Path, viewers_path: Path, run_dir: Path,
         done: list[Highlight], available_until: float | None,
         clip_vod: tuple[str, float] | None = None, dry_run: bool = False,
         publish: bool = True) -> list[Highlight] | None:
    """録画から作る。失敗したら None。"""
    chat = load_chat(chat_path) if chat_path.exists() and chat_path.stat().st_size else []
    viewers = load_viewers(viewers_path) if viewers_path.exists() and viewers_path.stat().st_size else None
    try:
        result = process(
            cfg, LocalSource(str(video)), chat, run_dir, channel=channel,
            exclude=done, available_until=available_until, viewers=viewers, clip_vod=clip_vod,
            dry_run=dry_run, publish=publish,
        )
    except Exception as e:
        log.warning("処理に失敗しました: %s", e)
        return None
    for h in result.highlights:
        log.info("作成: %s  %s", h.output_path, h.title)
    return result.highlights


# 停止の指示 (SIGTERM など) を受けたか。処理中に受けた場合は処理を終えてから止まる
_STOP = threading.Event()
_BUSY_LOCK = threading.Lock()
_BUSY = [0]  # 処理中の数 (本体と、前回の残りを処理する裏のスレッド)


def _busy_count() -> int:
    with _BUSY_LOCK:
        return _BUSY[0]


@contextmanager
def _busy():
    """この間は停止の指示を受けても処理を中断しない (書き出し途中で止まると成果物が残らないため)。"""
    with _BUSY_LOCK:
        _BUSY[0] += 1
    try:
        yield
    finally:
        with _BUSY_LOCK:
            _BUSY[0] -= 1


def _stop_on_terminate() -> None:
    """サービスの停止や PC のシャットダウン (SIGTERM / Windows の Ctrl+Break) を受けたら止まる。

    録画中・待機中なら Ctrl+C と同じく、ここまでの録画を処理して終了する。
    処理中なら、その処理が終わってから終了する。
    """
    def handler(signum, frame):
        _STOP.set()
        if _busy_count():
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


MAX_LEFTOVER_ATTEMPTS = 2


def _mark(session_dir: Path, name: str) -> None:
    try:
        (session_dir / name).write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
    except OSError:
        pass


def process_leftovers(cfg: Config, channel: str, dry_run: bool = False) -> None:
    """前回、最後まで処理する前に止まってしまった録画 (停電・強制終了・処理の失敗など) を処理する。

    対象は "processed" の印が無く、正式版の結果 (highlights.json) も無い録画。録画が途切れて次の録画で
    まとめて作る分 ("continued") は対象外。壊れた録画で毎回失敗し続けないよう、試すのは 2 回まで。
    """
    base = Path(cfg.work_dir) / channel
    for session_dir in sorted(base.glob("*/")) if base.exists() else []:
        if _STOP.is_set():
            return
        video = session_dir / "stream.ts"
        run_dir = Path(cfg.output_dir) / channel / session_dir.name
        if any((session_dir / m).exists() for m in ("processed", "continued")) \
                or (run_dir / "highlights.json").exists() \
                or not video.exists() or video.stat().st_size == 0:
            continue
        attempts_file = session_dir / "attempts"
        try:
            attempts = int(attempts_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            attempts = 0
        if attempts >= MAX_LEFTOVER_ATTEMPTS:
            continue
        if not dry_run:
            attempts_file.write_text(str(attempts + 1), encoding="utf-8")
        log.info("前回処理されなかった録画を処理します: %s", video)
        with _busy():
            result = _run(cfg, channel, video, session_dir / "chat.jsonl", session_dir / "viewers.jsonl",
                          run_dir, [], None, None, dry_run)
        if result is not None and not dry_run:
            _mark(session_dir, "processed")


def watch(cfg: Config, channel: str, once: bool = False, dry_run: bool = False) -> None:
    _stop_on_terminate()
    # 前回の残りは裏で処理する (その間に配信が始まっても録画を始められるように)
    threading.Thread(target=process_leftovers, args=(cfg, channel, dry_run), daemon=True,
                     name="leftovers").start()
    checker = LiveChecker(cfg)
    while True:
        wait_until_live(cfg, channel, checker)
        log.info("%s が配信を開始しました", channel)
        record_and_process(cfg, channel, checker.helix, dry_run=dry_run)
        if once:
            return
        # 配信終了直後の再接続などで即座に再録画しないよう少し待つ
        time.sleep(cfg.watch.poll_interval)
