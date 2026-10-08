"""動かすのに必要なものが揃っているかを確認する (twitch-shorts doctor)。"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from dataclasses import dataclass

from .config import Config
from .twitch_auth import can_create_clips, load_token


@dataclass
class Check:
    name: str
    ok: bool | None  # None = 確認できなかった
    detail: str
    required: bool = True


def _has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _japanese_font() -> tuple[bool | None, str]:
    if not shutil.which("fc-list"):
        return None, "確認できません (fc-list がありません)。字幕が □ になる場合は日本語フォントを入れてください"
    out = subprocess.run(["fc-list", ":lang=ja", "family"], capture_output=True, text=True).stdout.strip()
    if out:
        return True, out.splitlines()[0].split(",")[0].replace("\\", "")
    return False, "日本語フォントが見つかりません (例: Noto Sans CJK JP を入れる)"


def run_checks(cfg: Config, channel: str = "", online: bool = False) -> list[Check]:
    checks: list[Check] = []
    ff = shutil.which("ffmpeg") and shutil.which("ffprobe")
    checks.append(Check("ffmpeg", bool(ff), shutil.which("ffmpeg") or "インストールして PATH を通してください"))
    streamlink = shutil.which("streamlink")
    ytdlp = _has_module("yt_dlp")
    want = cfg.watch.recorder
    if want == "streamlink":
        checks.append(Check("録画ツール (streamlink)", bool(streamlink), streamlink or "Streamlink をインストールしてください"))
    elif want == "yt-dlp":
        checks.append(Check("録画ツール (yt-dlp)", ytdlp, "yt-dlp" if ytdlp else "pip install yt-dlp"))
    else:
        checks.append(Check("録画ツール (streamlink / yt-dlp)", bool(streamlink or ytdlp),
                            streamlink or ("yt-dlp" if ytdlp else "pip install yt-dlp")))
    ok, detail = _japanese_font()
    checks.append(Check("日本語フォント", ok, detail, required=False))

    keys = bool(cfg.twitch.client_id and cfg.twitch.client_secret)
    checks.append(Check("Twitch API キー", keys,
                        "設定済み" if keys else "TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET を設定 (視聴者数・分析・クリップに必要)",
                        required=False))
    if keys and online and channel:
        try:
            from .twitch_api import HelixClient

            uid = HelixClient(cfg.twitch.client_id, cfg.twitch.client_secret).get_user_id(channel)
            checks.append(Check("Twitch API 接続", True, f"{channel} (ID {uid})", required=False))
        except Exception as e:
            checks.append(Check("Twitch API 接続", False, str(e)[:200], required=False))

    token = load_token(cfg.work_dir)
    if token:
        login = token.get("login", "?")
        if not can_create_clips(token):
            detail = "クリップ作成の許可がありません。twitch-shorts login をやり直してください"
        elif not can_create_clips(token, channel):
            detail = f"{login} でログイン中ですが、{channel} のクリップは配信者本人のアカウントでしか作れません"
        else:
            detail = f"{login} でログイン済み"
        checks.append(Check("Twitch ログイン (クリップ作成)", can_create_clips(token, channel), detail, required=False))
    else:
        checks.append(Check("Twitch ログイン (クリップ作成)", False,
                            "未ログイン。Twitch の公式クリップも作るなら twitch-shorts login", required=False))

    checks.append(Check("字幕 (faster-whisper)", _has_module("faster_whisper"),
                        "使える" if _has_module("faster_whisper") else "任意: pip install 'twitch-shorts[transcribe]'",
                        required=False))
    llm = _has_module("anthropic") and bool(os.environ.get("ANTHROPIC_API_KEY"))
    checks.append(Check("AI タイトル・引きの言葉 (Claude)", llm,
                        "使える" if llm else "任意: pip install 'twitch-shorts[llm]' と ANTHROPIC_API_KEY",
                        required=False))
    return checks


def print_checks(checks: list[Check]) -> bool:
    """結果を表示し、必須項目がすべて OK なら True を返す。"""
    for c in checks:
        mark = "OK " if c.ok else "?? " if c.ok is None else ("NG " if c.required else "-- ")
        print(f"  [{mark}] {c.name}: {c.detail}")
    return all(c.ok for c in checks if c.required)
