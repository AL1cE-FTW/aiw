"""配信者が付けたストリームマーカーを取得する。

使用 API: GET https://api.twitch.tv/helix/streams/markers (Get Stream Markers)
  - user:read:broadcast スコープ付きのユーザートークンが必要 (VOD の持ち主か編集者)
  - video_id を指定すると、その VOD のマーカーを返す。position_seconds は配信の頭からの秒数
  - マーカーは配信中に Twitch のダッシュボードや /marker コマンドで付けられる
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .twitch_auth import MARKER_SCOPE, TwitchAuthError, UserToken, load_token

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"


@dataclass
class Marker:
    position: float  # VOD の頭からの秒数
    description: str = ""


def get_markers(client_id: str, token: UserToken, vod_id: str, session: requests.Session | None = None,
                max_pages: int = 10) -> list[Marker]:
    session = session or requests.Session()
    params = {"video_id": vod_id, "first": 100}
    markers: list[Marker] = []
    refreshed = False
    pages = 0
    while pages < max_pages:
        headers = {"Client-Id": client_id, "Authorization": f"Bearer {token.access_token()}"}
        r = session.get(f"{HELIX}/streams/markers", params=dict(params), headers=headers, timeout=30)
        if r.status_code == 401 and not refreshed:
            token.access_token(force_refresh=True)
            refreshed = True
            continue
        if r.status_code == 404:  # マーカーが 1 つも無い VOD
            break
        if r.status_code != 200:
            raise RuntimeError(f"ストリームマーカーを取得できませんでした: {r.status_code} {r.text[:200]}")
        body = r.json()
        for user in body.get("data") or []:
            for video in user.get("videos") or []:
                for m in video.get("markers") or []:
                    try:
                        markers.append(Marker(float(m["position_seconds"]), str(m.get("description") or "")))
                    except (KeyError, TypeError, ValueError):
                        continue
        cursor = (body.get("pagination") or {}).get("cursor")
        pages += 1
        if not cursor:
            break
        params["after"] = cursor
    return sorted(markers, key=lambda m: m.position)


def markers_for_vod(client_id: str, client_secret: str, work_dir: str, vod_id: str) -> list[Marker]:
    """ログイン済みならこの VOD のマーカーを返す。使えない場合は理由をログに出して空を返す (処理は止めない)。"""
    token = load_token(work_dir)
    if not client_id or not token:
        return []
    if MARKER_SCOPE not in (token.get("scopes") or []):
        log.info("ストリームマーカーを使うには twitch-shorts login をもう一度実行してください (許可の追加が必要です)")
        return []
    try:
        markers = get_markers(client_id, UserToken(client_id, client_secret, work_dir), vod_id)
    except (TwitchAuthError, RuntimeError, requests.RequestException) as e:
        log.warning("ストリームマーカーを使わずに続けます: %s", e)
        return []
    if markers:
        log.info("ストリームマーカー %d 件の場面を候補に入れます", len(markers))
    return markers
