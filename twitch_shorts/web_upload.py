"""見つけた場面の一覧を Web ページ (worker/、Cloudflare Workers) に送る。

ページでは Twitch でログインした配信者だけが一覧を見られ、各場面のクリップをボタン 1 つで作れる
(Twitch の「Create Clip From VOD」を使うので、VOD がある配信だけが対象)。
"""

from __future__ import annotations

import logging
import re

import requests

from .config import Config
from .models import Highlight
from .twitch_clips import clip_window

log = logging.getLogger(__name__)

# 一覧に載せる候補の上限 (ショートにした場面 + 次点の候補)
MAX_ITEMS = 30


def build_payload(run_id: str, channel: str, stream_title: str, vod_id: str, vod_shift: float,
                  candidates: list[Highlight], shorts: list[Highlight]) -> dict:
    """送るデータ。時刻は VOD 上の秒。クリップの範囲 (終わり位置と長さ) もここで Twitch の制約に合わせておく。"""
    short_ids = {id(h) for h in shorts}
    items = []
    for h in sorted(candidates[:MAX_ITEMS], key=lambda h: h.start):
        end, duration = clip_window(h, vod_shift)
        items.append({
            "start": round(h.start + vod_shift, 1),
            "end": round(h.end + vod_shift, 1),
            "score": round(float(h.score), 2),
            "title": h.title,
            "hook": h.hook,
            "category": h.category,
            "reason": h.reason,
            "short": id(h) in short_ids,
            "clip_end": end,
            "clip_duration": duration,
            "twitch_clip": h.twitch_clip,
        })
    return {"id": run_id, "channel": channel, "stream_title": stream_title, "vod_id": vod_id, "items": items}


def run_id_for(vod_id: str, run_dir_name: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", f"{vod_id}-{run_dir_name}")[:120]


def upload_run(cfg: Config, payload: dict, session: requests.Session | None = None) -> bool:
    """送る。失敗してもショート作りは止めない (False を返すだけ)。"""
    url, token = cfg.web.url.rstrip("/"), cfg.web.upload_token
    if not url or not token:
        return False
    try:
        r = (session or requests).post(f"{url}/api/upload", json=payload, timeout=30,
                                       headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException as e:
        log.warning("場面の一覧を Web ページに送れませんでした: %s", e)
        return False
    if r.status_code != 200:
        log.warning("場面の一覧を Web ページに送れませんでした: %s %s", r.status_code, r.text[:200])
        return False
    log.info("場面の一覧を Web ページに送りました: %s", url)
    return True
