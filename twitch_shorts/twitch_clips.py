"""検出した場面を Twitch の公式クリップとして作る。

使用 API: POST https://api.twitch.tv/helix/videos/clips (Create Clip From VOD, 2025 年 12 月からオープンベータ)
  - 配信者 (または編集者) のユーザートークンと channel:manage:clips / editor:manage:clips スコープが必要
  - vod_offset はクリップの「終わり」の位置 (秒)。クリップは vod_offset - duration から始まる
  - duration は 5〜60 秒
  - 配信中でも、その配信の VOD (アーカイブ) に対して作成できる
  - 位置は数秒ずれることがあると報告されている (Twitch Developer Forums)
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import requests

from .models import Highlight
from .twitch_auth import UserToken

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"
MIN_CLIP, MAX_CLIP = 5.0, 60.0


class ClipError(RuntimeError):
    pass


class ClipCreator:
    def __init__(self, client_id: str, token: UserToken, session: requests.Session | None = None):
        self.client_id = client_id
        self.token = token
        self.session = session or requests.Session()

    def from_vod(self, vod_id: str, end_offset: float, duration: float, title: str = "") -> dict:
        duration = round(min(MAX_CLIP, max(MIN_CLIP, duration)), 1)
        params = {
            "broadcaster_id": self.token.user_id,
            "editor_id": self.token.user_id,
            "vod_id": vod_id,
            # vod_offset は整数で、duration 以上である必要がある
            "vod_offset": max(math.ceil(end_offset), math.ceil(duration)),
            "duration": duration,
        }
        if title:
            params["title"] = title[:100]
        refreshed = retried_title = False
        for _ in range(4):
            headers = {"Client-Id": self.client_id, "Authorization": f"Bearer {self.token.access_token()}"}
            r = self.session.post(f"{HELIX}/videos/clips", params=params, headers=headers, timeout=30)
            if r.status_code in (200, 202):
                data = r.json().get("data") or []
                if not data:
                    raise ClipError("クリップ作成の応答が空でした")
                return data[0]
            if r.status_code == 401 and not refreshed:
                self.token.access_token(force_refresh=True)
                refreshed = True
                continue
            if r.status_code == 400 and "title" in params and not retried_title and "title" in r.text.lower():
                # オープンベータのため title を受け付けない場合に備え、無しで再試行する
                params.pop("title")
                retried_title = True
                continue
            if r.status_code == 429:
                reset = float(r.headers.get("Ratelimit-Reset", time.time() + 5))
                time.sleep(max(1.0, min(reset - time.time(), 60)))
                continue
            if r.status_code == 403:
                raise ClipError("クリップを作る権限がありません (自分のチャンネルか、編集者になっているチャンネルか確認してください)")
            raise ClipError(f"クリップを作成できませんでした: {r.status_code} {r.text[:200]}")
        raise ClipError("クリップを作成できませんでした (再試行の上限)")


def clip_window(h: Highlight, shift: float = 0.0) -> tuple[float, float]:
    """ハイライトを Twitch のクリップの制約 (5〜60 秒、終わり位置指定) に合わせる。

    shift: ハイライトの時刻に足すと VOD 上の時刻になる秒数 (録画開始が配信開始より遅れた分)。
    戻り値: (VOD 上の終わり位置, 長さ)
    """
    start, end = h.start + shift, h.end + shift
    duration = end - start
    if duration > MAX_CLIP:
        # 長すぎる場合は盛り上がりのピークを含むように後ろ寄せで 60 秒にする
        end = min(end, max(h.peak + shift + 15, start + MAX_CLIP))
        duration = MAX_CLIP
    duration = max(MIN_CLIP, duration)
    end = max(end, duration)  # vod_offset >= duration が必要
    return end, duration


def _load_registry(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []


def _find_existing(registry: list[dict], vod_id: str, end: float) -> dict | None:
    """同じ VOD のほぼ同じ位置 (終わり位置が 10 秒以内) に、以前作ったクリップがあれば返す。"""
    return next((r for r in registry if r.get("vod_id") == vod_id and abs(float(r.get("end", -1e9)) - end) < 10), None)


def create_clips(creator: ClipCreator, vod_id: str, highlights: list[Highlight],
                 shift: float = 0.0, limit: int = 5, registry: Path | None = None) -> list[Highlight]:
    """ハイライトごとにクリップを作り、URL を ``h.twitch_clip`` に記録する。失敗しても他は続ける。

    registry: 作ったクリップの記録 (JSON)。同じ VOD を処理し直しても同じ場面のクリップを重複して作らない。
    """
    known = _load_registry(registry)
    made: list[Highlight] = []
    for h in sorted(highlights, key=lambda h: h.score, reverse=True)[:limit]:
        if h.twitch_clip:
            continue
        end, duration = clip_window(h, shift)
        existing = _find_existing(known, vod_id, end)
        if existing:
            h.twitch_clip = existing["url"]
            log.info("作成済みの Twitch クリップを使います: %s", h.twitch_clip)
            continue
        try:
            clip = creator.from_vod(vod_id, end, duration, h.title)
        except ClipError as e:
            log.warning("Twitch クリップを作れませんでした (%s): %s", h.title, e)
            if "権限" in str(e):
                break
            continue
        except (requests.RequestException, ValueError, KeyError) as e:  # 通信エラー・想定外の応答
            log.warning("Twitch クリップを作れませんでした (%s): %s", h.title, e)
            continue
        h.twitch_clip = clip.get("edit_url") or f"https://clips.twitch.tv/{clip.get('id', '')}"
        log.info("Twitch クリップを作成: %s  %s", h.title, h.twitch_clip)
        made.append(h)
        known.append({"vod_id": vod_id, "end": round(end, 1), "duration": duration, "url": h.twitch_clip,
                      "id": clip.get("id", ""), "title": h.title})
        if registry:
            registry.parent.mkdir(parents=True, exist_ok=True)
            registry.write_text(json.dumps(known, ensure_ascii=False, indent=2), encoding="utf-8")
    return made
