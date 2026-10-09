"""作ったショート動画の投稿予定表。

参考: タルレミ・エラ「OW動画投稿講座」— 基本は毎日同じ時間に投稿し、解説系以外は 1 日 1 本までにする。
そのため、作成したショートは良い順に「毎日決まった時刻」の枠へ 1 本ずつ割り当てる。
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .fileutil import file_lock, read_json, read_list_for_update, write_json_atomic, write_text_atomic
from .models import Highlight

SCHEDULE_JSON = "schedule.json"
SCHEDULE_CSV = "schedule.csv"


def _paths(cfg: Config) -> tuple[Path, Path]:
    out = Path(cfg.output_dir)
    return out / SCHEDULE_JSON, out / SCHEDULE_CSV


def load_schedule(cfg: Config) -> list[dict]:
    """投稿予定表の有効な行 (手で編集して形の崩れた行は飛ばす)。"""
    path, _ = _paths(cfg)
    data = read_json(path, [])
    return [e for e in data if _valid_entry(e)] if isinstance(data, list) else []


def _valid_entry(e) -> bool:
    if not isinstance(e, dict) or not isinstance(e.get("path"), str) or not isinstance(e.get("title"), str):
        return False
    try:
        return datetime.fromisoformat(e["publish_at"]).tzinfo is not None  # 時差の無い日時は比べられない
    except (KeyError, TypeError, ValueError):
        return False


def _slot_times(cfg: Config, day: date, tz: ZoneInfo) -> list[datetime]:
    hh, mm = (int(x) for x in cfg.publish.post_time.split(":"))
    first = datetime.combine(day, time(hh, mm), tz)
    per_day = max(1, cfg.publish.posts_per_day)
    step = timedelta(hours=24 / per_day)
    return [first + step * i for i in range(per_day)]


def add_to_schedule(cfg: Config, highlights: list[Highlight], channel: str = "",
                    now: datetime | None = None) -> list[dict]:
    """書き出し済みのハイライトを空いている投稿枠に入れ、追加した予定を返す (別プロセスと同時でも安全)。"""
    json_path, _ = _paths(cfg)
    with file_lock(json_path):
        return _add_to_schedule(cfg, highlights, channel, now)


def _add_to_schedule(cfg: Config, highlights: list[Highlight], channel: str,
                     now: datetime | None) -> list[dict]:
    tz = ZoneInfo(cfg.publish.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    # 壊れていれば別名に移して作り直す。形の崩れた行は計算には使わないが、消さずに書き戻す
    raw = read_list_for_update(_paths(cfg)[0])
    entries = [e for e in raw if _valid_entry(e)]
    invalid = [e for e in raw if not _valid_entry(e)]
    known = {e["path"] for e in entries}
    used = Counter(datetime.fromisoformat(e["publish_at"]).astimezone(tz).isoformat() for e in entries)

    added: list[dict] = []
    # 1 日複数本のとき、前日の枠の一部は今日の早い時間にある (例: 19:00 と翌 07:00) ので前日から探す
    day = now.date() - timedelta(days=1)
    for h in sorted(highlights, key=lambda h: h.score, reverse=True):
        if not h.output_path or h.output_path in known:
            continue
        slot = None
        while slot is None:
            for t in _slot_times(cfg, day, tz):
                if t > now and not used[t.isoformat()]:
                    slot = t
                    break
            else:
                day += timedelta(days=1)
        used[slot.isoformat()] += 1
        entry = {
            "publish_at": slot.isoformat(),
            "title": h.title,
            "hook": h.hook,
            "category": h.category,
            "description": h.description,
            "hashtags": " ".join(h.hashtags),
            "twitch_clip": h.twitch_clip,
            "channel": channel,
            "score": h.score,
            "duration": round(h.video_duration or h.duration, 1),
            "path": h.output_path,
            "signals": h.signals,  # YouTube の結果と突き合わせて検出を調整するため (feedback コマンド)
        }
        entries.append(entry)
        added.append(entry)

    entries.sort(key=lambda e: e["publish_at"])
    json_path, csv_path = _paths(cfg)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(json_path, entries + invalid)  # 逐次処理のたびに書き換えるので、途中で止まっても壊れないように
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["publish_at", "title", "hook", "category", "description", "hashtags",
                                        "channel", "score", "duration", "path", "twitch_clip"],
                       extrasaction="ignore")
    w.writeheader()
    w.writerows(entries)
    write_text_atomic(csv_path, buf.getvalue(), encoding="utf-8-sig")  # Excel でも文字化けしないよう BOM 付き
    return added
