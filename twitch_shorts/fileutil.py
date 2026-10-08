"""ファイル書き込みの共通処理。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def write_json_atomic(path: str | Path, data, private: bool = False) -> None:
    """JSON を一時ファイルに書いてから置き換える (途中で止まっても元のファイルが壊れない)。

    一時ファイルは毎回新しく作るので、同時に書き込む別プロセスと混ざらない。
    private: 自分だけが読める権限 (0600) にする (Windows では権限指定は無視される)。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)  # 常に 0600 で新規作成
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        if not private:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def file_lock(path: str | Path, timeout: float = 120.0, stale: float = 600.0):
    """別のプロセスと同じファイルを読み書きするときの簡易ロック (``<path>.lock`` を作る)。

    ロックが stale 秒より古い (前回の異常終了の残り) 場合だけ奪う。使用中のロックは奪わず、
    timeout 秒待っても取れなければ TimeoutError。
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                too_old = time.time() - lock.stat().st_mtime > stale
            except OSError:
                continue  # ちょうど消えた
            if too_old:
                # 古いロックは名前を変えてから消す (rename は 1 つのプロセスしか成功しないので、
                # 複数のプロセスが同時に「古い」と判断しても、新しく作られたロックを消してしまわない)
                grave = lock.with_name(f"{lock.name}.stale.{os.getpid()}.{time.monotonic_ns()}")
                try:
                    os.rename(lock, grave)
                    os.unlink(grave)
                except OSError:
                    pass
                continue
            if time.time() > deadline:
                raise TimeoutError(f"{lock} を使っている別の処理が終わりません")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
