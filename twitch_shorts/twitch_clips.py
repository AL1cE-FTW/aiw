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

from .fileutil import file_lock, write_json_atomic
from .models import Highlight
from .twitch_auth import TwitchAuthError, UserToken

log = logging.getLogger(__name__)

HELIX = "https://api.twitch.tv/helix"
MIN_CLIP, MAX_CLIP = 5.0, 60.0


class ClipError(RuntimeError):
    pass


class ClipPermissionError(ClipError):
    """このアカウントでは (このチャンネルの) クリップを作れない。以降の試行も無駄なので止める。"""


class ClipCreator:
    def __init__(self, client_id: str, token: UserToken, session: requests.Session | None = None):
        self.client_id = client_id
        self.token = token
        self.session = session or requests.Session()

    def from_vod(self, vod_id: str, end_offset: float, duration: float, title: str = "") -> dict:
        end_offset, duration = normalize_window(end_offset, duration)
        params = {
            "broadcaster_id": self.token.user_id,
            "editor_id": self.token.user_id,
            "vod_id": vod_id,
            "vod_offset": end_offset,
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
                raise ClipPermissionError("クリップを作る権限がありません (自分のチャンネルか、編集者になっているチャンネルか確認してください)")
            raise ClipError(f"クリップを作成できませんでした: {r.status_code} {r.text[:200]}")
        raise ClipError("クリップを作成できませんでした (再試行の上限)")


def normalize_window(end: float, duration: float) -> tuple[int, float]:
    """Twitch の制約に合わせる: 長さは 5〜60 秒 (0.1 秒単位)、終わり位置は整数秒で長さ以上。"""
    duration = round(min(MAX_CLIP, max(MIN_CLIP, duration)), 1)
    return max(math.ceil(end), math.ceil(duration)), duration


def clip_window(h: Highlight, shift: float = 0.0) -> tuple[int, float]:
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
    return normalize_window(end, duration)


def _load_registry(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("クリップの記録 (%s) を読めないため、空として扱います", path)
        return []
    if not isinstance(data, list):
        return []
    valid = []
    for r in data:
        try:
            if isinstance(r, dict) and r.get("vod_id") and r.get("url"):
                float(r.get("end"))
                valid.append(r)
        except (TypeError, ValueError):
            continue  # 壊れた行は無視する
    return valid


def clips_made_for(registry: Path | None, vod_id: str) -> int:
    """この VOD で以前に作ったクリップの数 (1 配信あたりの上限の計算用)。"""
    return sum(1 for r in _load_registry(registry) if r.get("vod_id") == vod_id)


def _find_existing(registry: list[dict], vod_id: str, end: float) -> dict | None:
    """同じ VOD のほぼ同じ位置 (終わり位置が 10 秒以内) に、以前作ったクリップがあれば返す。"""
    return next((r for r in registry if r.get("vod_id") == vod_id and abs(float(r.get("end", -1e9)) - end) < 10), None)


def public_clip_url(clip_id: str) -> str:
    return f"https://clips.twitch.tv/{clip_id}"


def create_clips(creator: ClipCreator | None, vod_id: str, highlights: list[Highlight],
                 shift: float = 0.0, limit: int = 5, registry: Path | None = None) -> list[Highlight]:
    """ハイライトごとにクリップを作り、公開 URL を ``h.twitch_clip`` に記録する。失敗しても他は続ける。

    registry: 作ったクリップの記録 (JSON)。同じ場面のクリップが記録にあれば作らずにそれを使う
              (上限 ``limit`` は新しく作る本数だけに数える)。
    creator が None なら、記録にあるクリップを結び付けるだけで新しくは作らない。
    """
    known = _load_registry(registry)
    pending: list[tuple[Highlight, float, float]] = []
    for h in sorted(highlights, key=lambda h: h.score, reverse=True):
        if h.twitch_clip:
            continue
        end, duration = clip_window(h, shift)
        existing = _find_existing(known, vod_id, end)
        if existing:
            h.twitch_clip = existing["url"]
            h.twitch_clip_edit = existing.get("edit_url", "")
            continue
        pending.append((h, end, duration))

    made: list[Highlight] = []
    for h, end, duration in pending:
        if creator is None or len(made) >= limit:
            break
        try:
            clip = creator.from_vod(vod_id, end, duration, h.title)
        except ClipPermissionError as e:
            log.warning("Twitch クリップを作れませんでした: %s", e)
            break
        except ClipError as e:
            log.warning("Twitch クリップを作れませんでした (%s): %s", h.title, e)
            continue
        except (requests.RequestException, ValueError, KeyError) as e:  # 通信エラー・想定外の応答
            log.warning("Twitch クリップを作れませんでした (%s): %s", h.title, e)
            continue
        except TwitchAuthError as e:  # トークンの更新に失敗 (パスワード変更などで無効になった)
            log.warning("Twitch クリップを作れませんでした。twitch-shorts login をやり直してください: %s", e)
            break
        except Exception as e:  # その他の想定外のエラーでも残りのクリップは続ける
            log.warning("Twitch クリップを作れませんでした (%s): %s", h.title, e)
            continue
        # 視聴者に共有できる公開 URL と、配信者用の編集ページを分けて持つ
        h.twitch_clip = public_clip_url(clip["id"]) if clip.get("id") else clip.get("edit_url", "")
        h.twitch_clip_edit = clip.get("edit_url", "")
        log.info("Twitch クリップを作成: %s  %s", h.title, h.twitch_clip)
        made.append(h)
        entry = {"vod_id": vod_id, "end": round(end, 1), "duration": duration, "url": h.twitch_clip,
                 "edit_url": h.twitch_clip_edit, "id": clip.get("id", ""), "title": h.title}
        known.append(entry)
        if registry:
            # 別のプロセスが同時に書き足していても消さないよう、読み直してから追記する
            with file_lock(registry):
                write_json_atomic(registry, [*_load_registry(registry), entry])
    return made
