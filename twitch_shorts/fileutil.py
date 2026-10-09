"""ファイル書き込みの共通処理。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


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
            os.chmod(tmp, 0o666 & ~_UMASK)  # 普通にファイルを作ったときと同じ権限にする
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace(src: str, dst: Path) -> None:
    """os.replace。Windows では別の処理が読んでいる間は置き換えられないので、少し待ってやり直す。"""
    for i in range(10):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != "nt" or i == 9:
                raise
            time.sleep(0.2)


def _read_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


# 起動時に 1 回だけ読む (os.umask は一時的にプロセス全体の設定を変えるため、スレッドが動いてから呼ばない)
_UMASK = _read_umask()


def read_json(path: str | Path, default=None):
    """JSON ファイルを読む。無い・読めない・壊れている場合は default を返す。"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# latest / auto で処理済みの VOD の ID の一覧 (work_dir 直下)
PROCESSED_VODS = "processed_vods.json"


def processed_vods(work_dir: str | Path) -> set[str]:
    data = read_json(Path(work_dir) / PROCESSED_VODS, [])
    return {str(v) for v in data} if isinstance(data, list) else set()


def read_list_for_update(path: str | Path) -> list:
    """書き足す前に JSON の配列を読む (ロックを持っているときに使う)。

    読めない (OSError) ときは例外のまま返す (空として上書きすると、これまでの記録が消えるため)。
    壊れている・配列でないときは別名 (``<名前>.broken-<時刻>``) に移して残し、空から作り直す。
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except ValueError:  # JSON の誤り・文字コードの誤り
        pass
    backup = path.with_name(f"{path.name}.broken-{int(time.time())}")
    os.replace(path, backup)
    log.warning("%s が壊れているため %s に移して、新しく記録します", path, backup.name)
    return []


def add_processed_vod(work_dir: str | Path, vod_id: str) -> None:
    """処理済みの VOD を記録する (別のプロセスが同時に書き足していても消さない)。"""
    path = Path(work_dir) / PROCESSED_VODS
    with file_lock(path):
        done = {str(v) for v in read_list_for_update(path)}
        write_json_atomic(path, sorted(done | {vod_id}))


def read_jsonl(text: str) -> list[dict]:
    """1 行 1 JSON のテキストを読む。記録中の停電などで壊れた行や、オブジェクトでない行は飛ばす。"""
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def file_lock(path: str | Path, timeout: float = 120.0):
    """別のプロセス・スレッドと同じファイルを読み書きするときのロック (``<path>.lock`` に OS のロックをかける)。

    OS のロックなので、持っていたプロセスが異常終了しても自動で解放される (古いロックが残らない)。
    timeout 秒待っても取れなければ TimeoutError。
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.time() + timeout
        while not _try_lock(fd):
            if time.time() > deadline:
                raise TimeoutError(f"{lock} を使っている別の処理が終わりません")
            time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
