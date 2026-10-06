"""同時視聴者数の記録と読み込み。

Twitch API では過去の視聴者数の推移は取得できないため、watch モードの配信中に
Helix ``Get Streams`` の ``viewer_count`` を定期的に取得して自前で記録する。
"""

from __future__ import annotations

import csv
import json
import logging
import threading
import time
from pathlib import Path

from .models import ViewerSample

log = logging.getLogger(__name__)


def load_viewers(path: str | Path) -> list[ViewerSample]:
    """JSONL (``{"offset", "viewers"}``)、JSON 配列、CSV (offset,viewers) に対応。"""
    p = Path(path)
    raw = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".csv":
        rows = csv.DictReader(raw.splitlines())
        samples = [ViewerSample(float(r["offset"]), int(float(r["viewers"]))) for r in rows]
    else:
        try:
            data = json.loads(raw)
            items = data if isinstance(data, list) else [data]
        except json.JSONDecodeError:
            items = [json.loads(line) for line in raw.splitlines() if line.strip()]
        samples = [ViewerSample(float(d["offset"]), int(d["viewers"])) for d in items]
    return sorted(samples, key=lambda v: v.offset)


class ViewerRecorder:
    """配信中の同時視聴者数を一定間隔で JSONL に追記する。"""

    def __init__(self, helix, channel: str, out_path: str | Path, start_time: float, interval: float = 60):
        self.helix = helix
        self.channel = channel
        self.out_path = Path(out_path)
        self.start_time = start_time
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.count = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def sample_once(self) -> ViewerSample | None:
        stream = self.helix.get_stream(self.channel)
        if not stream:
            return None
        sample = ViewerSample(round(time.time() - self.start_time, 1), int(stream.get("viewer_count", 0)))
        with open(self.out_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"offset": sample.offset, "viewers": sample.viewers}) + "\n")
        self.count += 1
        return sample

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception as e:  # 一時的な API エラーで記録を止めない
                log.debug("視聴者数の取得に失敗: %s", e)
            self._stop.wait(self.interval)
