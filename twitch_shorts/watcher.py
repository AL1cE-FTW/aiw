"""チャンネルを監視し、配信が始まったら録画とチャット記録を行い、自動でショート動画を作る。"""

from __future__ import annotations

import logging
import signal
import time
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
# 配信の VOD が見つからなかったときに探し直す間隔 (秒)
VOD_RETRY_SECONDS = 600


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
        time.sleep(cfg.watch.poll_interval)


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

    vod_state: dict = {"vod": None, "user_id": None, "next_try": 0.0, "warned": False,
                       "started": stream_started}

    def clip_vod(force: bool = False) -> tuple[str, float] | None:
        """この配信の VOD を探す (クリップ作成と、場面へのリンク用)。

        見つかるまでは 10 分おきに探し直し、見つからない旨の警告は 1 回だけ出す。
        force: 待ち時間を無視して探す (配信終了後の最後の処理用)。
        """
        if helix is None or vod_state["vod"] is not None:
            return vod_state["vod"]
        if not force and time.time() < vod_state["next_try"]:
            return None
        vod_state["next_try"] = time.time() + VOD_RETRY_SECONDS
        try:
            if vod_state["started"] is None:
                vod_state["started"] = _stream_started_at(helix, channel)
            if vod_state["user_id"] is None:
                vod_state["user_id"] = helix.get_user_id(channel)
            # 録画は配信より少し遅れて届くので、その分だけ VOD 上では前の位置になる
            found = find_stream_vod(helix, channel, start_time - cfg.watch.stream_latency,
                                    vod_state["started"], vod_state["user_id"])
        except Exception as e:
            log.warning("この配信の VOD を確認できませんでした: %s", e)
            return None
        if not found:
            if not vod_state["warned"]:
                log.warning("この配信の VOD が見つかりません。Twitch クリップと VOD へのリンクは付きません "
                            "(Twitch の設定で「過去の配信を保存」が有効か確認してください)")
                vod_state["warned"] = True
            return None
        vod_state["vod"] = found
        return found

    done: list[Highlight] = []
    interrupted = False
    next_run = start_time + cfg.watch.rolling_minutes * 60
    try:
        while proc.poll() is None:
            time.sleep(5)
            if cfg.watch.rolling_minutes > 0 and time.time() >= next_run and video_path.exists():
                next_run = time.time() + cfg.watch.rolling_minutes * 60
                elapsed = time.time() - start_time
                done += _run(cfg, channel, video_path, chat_path, viewers_path, run_dir, done,
                             elapsed - LIVE_TAIL_MARGIN, clip_vod(), dry_run)
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
    log.info("配信終了。チャット %d 件。最終処理を行います", chat_rec.count)
    done += _run(cfg, channel, video_path, chat_path, viewers_path, run_dir, done, None, clip_vod(force=True),
                 dry_run)
    if interrupted:
        # Ctrl+C は「止める」意味なので、配信が続いていても次の録画は始めずに終了する
        raise KeyboardInterrupt
    return done


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
         clip_vod: tuple[str, float] | None = None, dry_run: bool = False) -> list[Highlight]:
    chat = load_chat(chat_path) if chat_path.exists() and chat_path.stat().st_size else []
    viewers = load_viewers(viewers_path) if viewers_path.exists() and viewers_path.stat().st_size else None
    try:
        result = process(
            cfg, LocalSource(str(video)), chat, run_dir, channel=channel,
            exclude=done, available_until=available_until, viewers=viewers, clip_vod=clip_vod,
            dry_run=dry_run,
        )
    except Exception as e:
        log.warning("処理に失敗しました (次回に再試行します): %s", e)
        return []
    for h in result.highlights:
        log.info("作成: %s  %s", h.output_path, h.title)
    return result.highlights


def _stop_on_terminate() -> None:
    """サービスの停止や PC のシャットダウン (SIGTERM / Windows の Ctrl+Break) を Ctrl+C と同じに扱う。

    そうしないと、録画途中の分を処理せずにすぐ終了してしまう。
    """
    def handler(signum, frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # メインスレッド以外など
                pass


def process_leftovers(cfg: Config, channel: str, dry_run: bool = False) -> None:
    """前回、処理する前に止まってしまった録画 (停電・強制終了など) を処理する。"""
    base = Path(cfg.work_dir) / channel
    for session_dir in sorted(base.glob("*/")) if base.exists() else []:
        video = session_dir / "stream.ts"
        run_dir = Path(cfg.output_dir) / channel / session_dir.name
        if not video.exists() or video.stat().st_size == 0 or (run_dir / "highlights.json").exists():
            continue
        log.info("前回処理されなかった録画を処理します: %s", video)
        _run(cfg, channel, video, session_dir / "chat.jsonl", session_dir / "viewers.jsonl", run_dir, [], None,
             None, dry_run)


def watch(cfg: Config, channel: str, once: bool = False, dry_run: bool = False) -> None:
    _stop_on_terminate()
    process_leftovers(cfg, channel, dry_run)
    checker = LiveChecker(cfg)
    while True:
        wait_until_live(cfg, channel, checker)
        log.info("%s が配信を開始しました", channel)
        record_and_process(cfg, channel, checker.helix, dry_run=dry_run)
        if once:
            return
        # 配信終了直後の再接続などで即座に再録画しないよう少し待つ
        time.sleep(cfg.watch.poll_interval)
