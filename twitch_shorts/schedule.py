"""作ったショート動画の投稿予定表。

参考: タルレミ・エラ「OW動画投稿講座」— 基本は毎日同じ時間に投稿し、解説系以外は 1 日 1 本までにする。
そのため、作成したショートは良い順に「毎日決まった時刻」の枠へ 1 本ずつ割り当てる。
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .models import Highlight

SCHEDULE_JSON = "schedule.json"
SCHEDULE_CSV = "schedule.csv"


def _paths(cfg: Config) -> tuple[Path, Path]:
    out = Path(cfg.output_dir)
    return out / SCHEDULE_JSON, out / SCHEDULE_CSV


def load_schedule(cfg: Config) -> list[dict]:
    path, _ = _paths(cfg)
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _slot_times(cfg: Config, day: date, tz: ZoneInfo) -> list[datetime]:
    hh, mm = (int(x) for x in cfg.publish.post_time.split(":"))
    first = datetime.combine(day, time(hh, mm), tz)
    per_day = max(1, cfg.publish.posts_per_day)
    step = timedelta(hours=24 / per_day)
    return [first + step * i for i in range(per_day)]


def add_to_schedule(cfg: Config, highlights: list[Highlight], channel: str = "",
                    now: datetime | None = None) -> list[dict]:
    """書き出し済みのハイライトを空いている投稿枠に入れ、追加した予定を返す。"""
    tz = ZoneInfo(cfg.publish.timezone)
    now = (now or datetime.now(tz)).astimezone(tz)
    entries = load_schedule(cfg)
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
    json_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:  # Excel でも文字化けしないよう BOM 付き
        w = csv.DictWriter(f, fieldnames=["publish_at", "title", "hook", "category", "description", "hashtags",
                                          "channel", "score", "duration", "path", "twitch_clip"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(entries)
    return added
