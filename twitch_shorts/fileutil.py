"""ファイル書き込みの共通処理。"""

from __future__ import annotations

import json
import os
from pathlib import Path


def write_json_atomic(path: str | Path, data, private: bool = False) -> None:
    """JSON を一時ファイルに書いてから置き換える (途中で止まっても元のファイルが壊れない)。

    private: 最初から自分だけが読める権限 (0600) で作る (Windows では権限指定は無視される)。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o666)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)
