"""Twitch 公式 Helix API の薄いクライアント。

使用エンドポイント:
  - POST https://id.twitch.tv/oauth2/token        (Client Credentials で App Access Token を取得)
  - GET  https://api.twitch.tv/helix/users        (ログイン名 → broadcaster_id)
  - GET  https://api.twitch.tv/helix/videos       (VODの情報)
  - GET  https://api.twitch.tv/helix/clips        (視聴者が作ったクリップ。vod_offset でVOD上の位置が分かる)
  - GET  https://api.twitch.tv/helix/streams      (配信中かどうか)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from .models import ClipRef

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
HELIX = "https://api.twitch.tv/helix"


class TwitchAPIError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    id: str
    user_id: str
    user_login: str
    user_name: str
    title: str
    created_at: datetime
    duration: float
    url: str


_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


def parse_twitch_duration(text: str) -> float:
    """Helix の "3h8m33s" 形式を秒に変換する。"""
    m = _DURATION_RE.fullmatch(text.strip())
    if not m or not any(m.groups()):
        raise ValueError(f"解釈できない長さです: {text!r}")
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + s)


def parse_rfc3339(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _video_from_json(v: dict) -> VideoInfo:
    return VideoInfo(
        id=v["id"],
        user_id=v["user_id"],
        user_login=v["user_login"],
        user_name=v["user_name"],
        title=v["title"],
        created_at=parse_rfc3339(v["created_at"]),
        duration=parse_twitch_duration(v["duration"]),
        url=v["url"],
    )


_VOD_RE = re.compile(r"(?:twitch\.tv/(?:\w+/)?videos?/|^v?)(\d{6,})")


def parse_video_id(url_or_id: str) -> str:
    m = _VOD_RE.search(url_or_id.strip())
    if not m:
        raise ValueError(f"VOD ID を取得できません: {url_or_id!r}")
    return m.group(1)


class HelixClient:
    def __init__(self, client_id: str, client_secret: str, session: requests.Session | None = None):
        if not client_id or not client_secret:
            raise TwitchAPIError(
                "Twitch API の認証情報がありません。TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET を設定してください"
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expiry = 0.0

    # --- auth -------------------------------------------------------------
    def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        r = self.session.post(
            TOKEN_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise TwitchAPIError(f"トークン取得に失敗しました: {r.status_code} {r.text[:200]}")
        body = r.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + float(body.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: dict | list) -> dict:
        for attempt in range(2):
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self._ensure_token()}"}
            r = self.session.get(f"{HELIX}/{path}", params=params, headers=headers, timeout=30)
            if r.status_code == 401 and attempt == 0:
                self._token = None  # 期限切れトークンを捨てて再取得
                continue
            if r.status_code == 429 and attempt == 0:
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 1))
                time.sleep(max(0.0, min(reset - time.time(), 60)))
                continue
            if r.status_code != 200:
                raise TwitchAPIError(f"GET {path} が失敗しました: {r.status_code} {r.text[:200]}")
            return r.json()
        raise TwitchAPIError(f"GET {path} が失敗しました")

    # --- endpoints --------------------------------------------------------
    def get_user_id(self, login: str) -> str:
        data = self._get("users", {"login": login.lower()})["data"]
        if not data:
            raise TwitchAPIError(f"ユーザーが見つかりません: {login}")
        return data[0]["id"]

    def get_video(self, video_id: str) -> VideoInfo:
        data = self._get("videos", {"id": video_id})["data"]
        if not data:
            raise TwitchAPIError(f"VOD が見つかりません: {video_id}")
        return _video_from_json(data[0])

    def get_recent_archives(self, user_id: str, count: int = 5) -> list[VideoInfo]:
        """チャンネルの過去配信アーカイブ (新しい順)。"""
        data = self._get("videos", {"user_id": user_id, "type": "archive", "first": max(1, min(count, 100))})["data"]
        return [_video_from_json(v) for v in data]

    def get_stream(self, login: str) -> dict | None:
        """配信中ならストリーム情報 (viewer_count, title, started_at など)、オフラインなら None。"""
        data = self._get("streams", {"user_login": login.lower()})["data"]
        return data[0] if data else None

    def is_live(self, login: str) -> bool:
        return self.get_stream(login) is not None

    def get_clips(self, broadcaster_id: str, started_at: datetime, ended_at: datetime,
                  max_pages: int = 10) -> list[dict]:
        """期間内に作られたクリップ (生の JSON)。ended_at を省略すると 1 週間に制限されるので常に指定する。"""
        params = {
            "broadcaster_id": broadcaster_id,
            "started_at": to_rfc3339(started_at),
            "ended_at": to_rfc3339(ended_at),
            "first": 100,
        }
        out: list[dict] = []
        for _ in range(max_pages):
            body = self._get("clips", params)
            out.extend(body.get("data", []))
            cursor = body.get("pagination", {}).get("cursor")
            if not cursor:
                break
            params = {**params, "after": cursor}
        return out

    def get_clips_for_video(self, video: VideoInfo, max_pages: int = 10) -> list[ClipRef]:
        """VOD の配信時間帯に作られたクリップのうち、その VOD に紐づくものを返す。"""
        raw = self.get_clips(
            video.user_id,
            video.created_at,
            video.created_at + timedelta(seconds=video.duration + 3600),
            max_pages,
        )
        return [
            ClipRef(
                offset=float(c["vod_offset"]),
                duration=float(c.get("duration") or 30),
                views=int(c.get("view_count") or 0),
                title=c.get("title", ""),
                url=c.get("url", ""),
            )
            for c in raw
            if c.get("video_id") == video.id and c.get("vod_offset") is not None
        ]

    def get_game_names(self, game_ids: list[str]) -> dict[str, str]:
        ids = sorted({g for g in game_ids if g})
        names: dict[str, str] = {}
        for i in range(0, len(ids), 100):
            body = self._get("games", [("id", g) for g in ids[i : i + 100]])
            names.update({g["id"]: g["name"] for g in body.get("data", [])})
        return names
