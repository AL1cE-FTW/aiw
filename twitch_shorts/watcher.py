"""チャンネルを監視し、配信が始まったら録画とチャット記録を行い、自動でショート動画を作る。"""

from __future__ import annotations

import logging
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


def record_and_process(cfg: Config, channel: str, helix=None) -> list[Highlight]:
    """1 回分の配信を録画し、ショート動画を作る。配信が終わったら戻る。

    ``helix`` (HelixClient) があれば同時視聴者数も記録し、検出のシグナルに使う。
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
    viewer_rec = None
    if helix is not None:
        viewer_rec = ViewerRecorder(helix, channel, viewers_path, start_time, cfg.watch.viewer_poll_interval)
        viewer_rec.start()
    else:
        log.info("Twitch API の認証情報が無いため、同時視聴者数は記録しません")
    log.info("録画中: %s / チャット: %s", video_path, chat_path)

    vod_cache: dict[str, tuple[str, float]] = {}

    def clip_vod() -> tuple[str, float] | None:
        """この配信の VOD を探す (クリップ作成と、場面へのリンク用)。見つかるまで毎回探し直す。"""
        if helix is None:
            return None
        if "vod" not in vod_cache:
            try:
                found = find_stream_vod(helix, channel, start_time)
            except Exception as e:
                log.warning("この配信の VOD を確認できませんでした: %s", e)
                return None
            if not found:
                log.warning("この配信の VOD が見つかりません。Twitch クリップと VOD へのリンクは付きません "
                            "(Twitch の設定で「過去の配信を保存」が有効か確認してください)")
                return None
            vod_cache["vod"] = found
        return vod_cache["vod"]

    done: list[Highlight] = []
    next_run = start_time + cfg.watch.rolling_minutes * 60
    try:
        while proc.poll() is None:
            time.sleep(5)
            if cfg.watch.rolling_minutes > 0 and time.time() >= next_run and video_path.exists():
                next_run = time.time() + cfg.watch.rolling_minutes * 60
                elapsed = time.time() - start_time
                done += _run(cfg, channel, video_path, chat_path, viewers_path, run_dir, done,
                             elapsed - LIVE_TAIL_MARGIN, clip_vod())
    except KeyboardInterrupt:
        log.info("中断されました。録画を止めて、ここまでの分を処理します")
        proc.terminate()
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
        return done
    log.info("配信終了。チャット %d 件。最終処理を行います", chat_rec.count)
    done += _run(cfg, channel, video_path, chat_path, viewers_path, run_dir, done, None, clip_vod())
    return done


def find_stream_vod(helix, channel: str, record_start: float) -> tuple[str, float] | None:
    """録画中の配信の VOD と、録画の 0 秒が VOD の何秒目か (= 録画開始の遅れ) を返す。"""
    user_id = helix.get_user_id(channel)
    for v in helix.get_recent_archives(user_id, 3):
        started = v.created_at.timestamp()
        # 配信開始 (VOD の 0 秒) は録画開始より前で、同じ配信なら離れすぎていない
        if started <= record_start + 120 and record_start - started < 48 * 3600:
            return v.id, max(0.0, record_start - started)
    return None


def _run(cfg: Config, channel: str, video: Path, chat_path: Path, viewers_path: Path, run_dir: Path,
         done: list[Highlight], available_until: float | None,
         clip_vod: tuple[str, float] | None = None) -> list[Highlight]:
    chat = load_chat(chat_path) if chat_path.exists() and chat_path.stat().st_size else []
    viewers = load_viewers(viewers_path) if viewers_path.exists() and viewers_path.stat().st_size else None
    try:
        result = process(
            cfg, LocalSource(str(video)), chat, run_dir, channel=channel,
            exclude=done, available_until=available_until, viewers=viewers, clip_vod=clip_vod,
        )
    except Exception as e:
        log.warning("処理に失敗しました (次回に再試行します): %s", e)
        return []
    for h in result.highlights:
        log.info("作成: %s  %s", h.output_path, h.title)
    return result.highlights


def watch(cfg: Config, channel: str, once: bool = False) -> None:
    checker = LiveChecker(cfg)
    while True:
        wait_until_live(cfg, channel, checker)
        log.info("%s が配信を開始しました", channel)
        record_and_process(cfg, channel, checker.helix)
        if once:
            return
        # 配信終了直後の再接続などで即座に再録画しないよう少し待つ
        time.sleep(cfg.watch.poll_interval)
