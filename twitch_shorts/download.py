"""yt-dlp / streamlink を使った VOD のダウンロードとライブ録画。"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _ydl(opts: dict):
    import yt_dlp

    base = {"quiet": True, "no_warnings": True, "noprogress": True}
    base.update(opts)
    return yt_dlp.YoutubeDL(base)


def vod_url(video_id: str) -> str:
    return f"https://www.twitch.tv/videos/{video_id}"


def channel_url(channel: str) -> str:
    return f"https://www.twitch.tv/{channel.lower()}"


def download_audio(url: str, out_dir: str | Path) -> Path:
    """VOD の音声のみをダウンロードする (Twitch の Audio_Only 形式。無ければ最低画質)。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("audio.*"))
    if existing:
        return existing[0]
    with _ydl({"format": "bestaudio/worst", "outtmpl": str(out_dir / "audio.%(ext)s")}) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def download_video(url: str, out_dir: str | Path, quality: str = "best") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(out_dir.glob("video.*"))
    if existing:
        return existing[0]
    with _ydl({"format": quality, "outtmpl": str(out_dir / "video.%(ext)s")}) as ydl:
        info = ydl.extract_info(url, download=True)
        return Path(ydl.prepare_filename(info))


def download_section(url: str, start: float, end: float, out_path: str | Path, quality: str = "best") -> Path:
    """VOD の [start, end) だけをダウンロードする。キーフレーム合わせで再エンコードするので位置は正確。"""
    from yt_dlp.utils import download_range_func

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        return out_path
    opts = {
        "format": quality,
        "outtmpl": str(out_path.with_suffix("")) + ".%(ext)s",
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
    }
    with _ydl(opts) as ydl:
        ydl.extract_info(url, download=True)
    # 区間ダウンロードでは拡張子が変わることがあるので、実際にできたファイルを探す
    produced = [p for p in out_path.parent.glob(out_path.stem + ".*") if p.suffix not in (".part", ".ytdl")]
    if not produced:
        raise RuntimeError(f"区間のダウンロードに失敗しました: {start:.0f}-{end:.0f}s")
    if produced[0] != out_path:
        produced[0].rename(out_path)
    return out_path


def is_live_via_ytdlp(channel: str) -> bool:
    """API キー無しで配信中か調べる (オフラインだと yt-dlp がエラーを返す)。

    「オフライン」以外のエラー (通信の不調など) は例外のまま返す (呼び出し側で「分からない」として扱うため)。
    """
    from yt_dlp.utils import DownloadError

    try:
        with _ydl({"skip_download": True}) as ydl:
            info = ydl.extract_info(channel_url(channel), download=False)
        return bool(info and info.get("is_live", True))
    except DownloadError as e:
        msg = str(e).lower()
        if "offline" in msg or "not currently live" in msg or "does not exist" in msg:
            return False
        raise


def start_live_recording(channel: str, out_path: str | Path, recorder: str = "auto",
                         quality: str = "best") -> subprocess.Popen:
    """ライブ配信を MPEG-TS に録画するサブプロセスを起動する。配信終了でプロセスも終了する。

    MPEG-TS は書き込み途中でも読めるので、配信中に解析・切り抜きができる。
    """
    out_path = str(out_path)
    use_streamlink = recorder == "streamlink" or (recorder == "auto" and shutil.which("streamlink"))
    if use_streamlink:
        # 広告は録画しない (広告の映像がショートになると権利上の問題になる)。広告を飛ばした分だけ
        # 録画は短くなるが、配信後の最終処理は広告の無い VOD から行うので位置はずれない
        cmd = ["streamlink", "--twitch-disable-ads", "--force", "-o", out_path, channel_url(channel), quality]
    else:
        cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-part", "--hls-use-mpegts",
               "-f", quality, "-o", out_path, channel_url(channel)]
    log.info("録画開始: %s", " ".join(cmd))
    # 録画ツールのメッセージはファイルに書き出す (パイプのままだと、読まずにいると溜まって録画が止まるため)
    log_path = Path(out_path).with_suffix(".log")
    with open(log_path, "ab") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err)
    proc.log_path = log_path
    return proc


def recorder_log_tail(proc, size: int = 500) -> str:
    """録画ツールのメッセージの末尾 (録画が止まった理由を調べるため)。"""
    path = getattr(proc, "log_path", None)
    try:
        return Path(path).read_bytes()[-size:].decode(errors="replace") if path else ""
    except OSError:
        return ""
