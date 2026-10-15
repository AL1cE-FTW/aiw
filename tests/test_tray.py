import threading
import time
from pathlib import Path

import pytest

from twitch_shorts import tray, watcher


class FakeProc:
    def __init__(self):
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def make_ctl(tmp_path, procs):
    def popen(cmd, **kw):
        p = FakeProc()
        procs.append((cmd, kw, p))
        return p

    return tray.AutoController("yuuki_ftw", tmp_path / "work", ["config.toml"], cwd=tmp_path, popen=popen)


def test_start_runs_auto_with_stop_file_and_log(tmp_path):
    procs = []
    ctl = make_ctl(tmp_path, procs)
    assert ctl.status() == "stopped"
    assert ctl.start() is True and ctl.start() is False  # 二重には起動しない
    cmd, kw, proc = procs[0]
    assert cmd[-2:] == ["auto", "yuuki_ftw"] and "-c" in cmd
    assert kw["env"][watcher.STOP_FILE_ENV] == str(ctl.stop_file)
    assert kw["cwd"] == str(tmp_path.resolve())
    assert ctl.log_path.exists() and ctl.status() == "running"


def test_turning_off_asks_auto_to_finish_then_stop(tmp_path):
    procs = []
    ctl = make_ctl(tmp_path, procs)
    ctl.set_auto(True)
    ctl.set_auto(False)
    assert ctl.auto_enabled() is False
    assert ctl.stop_file.exists() and ctl.status() == "stopping"
    procs[0][2].returncode = 130  # auto が処理を終えて止まった
    assert ctl.check() is True
    assert ctl.status() == "stopped" and not ctl.stop_file.exists() and not ctl.error


def test_unexpected_exit_is_reported(tmp_path):
    procs = []
    ctl = make_ctl(tmp_path, procs)
    ctl.start()
    procs[0][2].returncode = 1
    ctl.check()
    assert ctl.status() == "error" and "1" in ctl.status_text()
    ctl.start()  # もう一度 ON にすればエラーの表示は消える
    assert ctl.status() == "running"


def test_kill_stops_immediately(tmp_path):
    procs = []
    ctl = make_ctl(tmp_path, procs)
    ctl.start()
    ctl.stop()
    ctl.kill()
    assert procs[0][2].killed


def test_auto_setting_is_remembered(tmp_path):
    ctl = make_ctl(tmp_path, [])
    assert ctl.auto_enabled() is True  # 初めては ON
    ctl.save_auto_enabled(False)
    assert make_ctl(tmp_path, []).auto_enabled() is False


def test_watch_stops_when_stop_file_appears(tmp_path, monkeypatch):
    stop = tmp_path / "tray.stop"
    watcher._STOP.clear()
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    t = threading.Thread(target=watcher._watch_stop_file, args=(stop,), daemon=True)
    t.start()
    stop.write_text("x")
    t.join(timeout=5)
    assert watcher._STOP.is_set()
    watcher._STOP.clear()


def test_startup_shortcut_points_to_tray(tmp_path, monkeypatch):
    monkeypatch.setattr(tray, "_startup_dir", lambda: tmp_path)
    (tmp_path / tray.OLD_STARTUP_NAME).write_text("old")  # .bat で登録した auto の自動起動
    calls = []

    def run(cmd, env=None, **kw):
        calls.append(env)
        Path(env["TS_LINK"]).write_text("lnk")

    tray.set_startup(True, "yuuki_ftw", ["my config.toml"], cwd=tmp_path, run=run)
    env = calls[0]
    assert env["TS_ARGS"].endswith("tray yuuki_ftw") and '"' in env["TS_ARGS"]  # 空白入りのパスは引用符で囲む
    assert env["TS_CWD"] == str(tmp_path.resolve())
    assert tray.startup_enabled() is True
    assert not (tmp_path / tray.OLD_STARTUP_NAME).exists()  # 二重に動かないよう古い登録は外す
    tray.set_startup(False, "yuuki_ftw", run=run)
    assert tray.startup_enabled() is False


def test_startup_is_windows_only(monkeypatch):
    monkeypatch.setattr(tray, "_startup_dir", lambda: None)
    with pytest.raises(RuntimeError):
        tray.set_startup(True, "yuuki_ftw")


def test_icon_images_for_each_state():
    pytest.importorskip("PIL")
    for status in tray.COLORS:
        img = tray.make_image(status)
        assert img.size == (64, 64)


def test_latest_review_page(tmp_path):
    a = tmp_path / "a" / "index.html"
    b = tmp_path / "b" / "index.html"
    for p in (a, b):
        p.parent.mkdir()
        p.write_text("x")
    import os
    os.utime(a, (time.time() - 100, time.time() - 100))
    assert tray.latest_review_page(tmp_path) == b
    assert tray.latest_review_page(tmp_path / "none") is None


def test_run_tray_menu_with_fake_pystray(tmp_path, monkeypatch):
    """pystray の代わりに模擬を使い、メニューで OFF → 終了するまでの流れを確かめる。"""
    import sys
    import types
    from types import SimpleNamespace

    pytest.importorskip("PIL")
    real_sleep = time.sleep
    fake = types.ModuleType("pystray")
    procs = []

    def wait_for(cond):
        for _ in range(500):
            if cond():
                return
            real_sleep(0.01)
        raise AssertionError("待ちきれませんでした")

    class MenuItem:
        def __init__(self, text, action, checked=None, radio=False, default=False, visible=True, enabled=True):
            self.text, self.action, self.checked, self.visible, self.enabled = text, action, checked, visible, enabled

    class Menu:
        SEPARATOR = MenuItem("-", None)

        def __init__(self, *items):
            self.items = items

    class Icon:
        def __init__(self, name, image, title, menu):
            self.menu, self.title, self.visible, self.stopped = menu, title, False, False
            fake.last = self

        def update_menu(self):
            pass

        def notify(self, message, title=None):
            pass

        def stop(self):
            self.stopped = True

        def run(self, setup):
            t = threading.Thread(target=setup, args=(self,), daemon=True)
            t.start()
            item = {i.text: i for i in self.menu.items if isinstance(i.text, str)}
            auto = item["自動処理 (配信ごとにショートを作る)"]
            wait_for(lambda: procs and auto.checked(auto))  # 起動時に ON なので auto が動いている
            auto.action(self, None)  # OFF にする → 止めている途中
            wait_for(lambda: (tmp_path / "work" / "tray.stop").exists())
            procs[0].returncode = 130  # auto がいまの処理を終えて止まった
            item["終了"].action(self, None)
            t.join(timeout=5)

    fake.Menu, fake.MenuItem, fake.Icon = Menu, MenuItem, Icon
    monkeypatch.setitem(sys.modules, "pystray", fake)
    monkeypatch.setattr(tray, "time", SimpleNamespace(sleep=lambda s: real_sleep(0.01), time=time.time))

    class Ctl(tray.AutoController):
        def __init__(self, *a, **k):
            def popen(cmd, **kw):
                p = FakeProc()
                procs.append(p)
                return p
            super().__init__(*a, popen=popen, **k)

    monkeypatch.setattr(tray, "AutoController", Ctl)
    timers = []

    class Timer:  # 5 秒後に os._exit する保険のタイマー。本物だとテストのプロセスごと終わる
        def __init__(self, interval, fn, args=()):
            self.daemon, self.started, self.cancelled = False, False, False
            timers.append(self)

        def start(self):
            self.started = True

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(threading, "Timer", Timer)
    work = tmp_path / "work"
    work.mkdir()
    assert tray.run_tray("yuuki_ftw", str(work), str(tmp_path / "out")) == 0
    assert fake.last.stopped and len(procs) == 1  # OFF にしたので起動し直さない
    # アイコンが普通に閉じたら、強制終了のタイマーは止める (後片付けを途中で切らない)
    assert timers and all(t.started and t.cancelled for t in timers)
    assert '"auto": false' in (work / "tray.json").read_text()


def test_turning_on_while_stopping_restarts_after_exit(tmp_path):
    procs = []
    ctl = make_ctl(tmp_path, procs)
    ctl.set_auto(True)
    ctl.set_auto(False)
    ctl.set_auto(True)  # 止まりきる前に ON に戻した
    procs[0][2].returncode = 130
    ctl.check()
    assert ctl.keep_alive() is True and len(procs) == 2 and ctl.status() == "running"


def test_error_stop_is_not_restarted_automatically(tmp_path):
    procs = []
    ctl = make_ctl(tmp_path, procs)
    ctl.set_auto(True)
    procs[0][2].returncode = 1
    ctl.check()
    assert ctl.keep_alive() is False and ctl.status() == "error"


def test_does_not_start_when_another_auto_holds_the_lock(tmp_path):
    from twitch_shorts.fileutil import file_lock

    ctl = make_ctl(tmp_path, [])
    (tmp_path / "work").mkdir()
    import subprocess
    import sys as _sys
    holder = subprocess.Popen([_sys.executable, "-c", (
        "import sys, time; sys.path.insert(0, %r);"
        "from twitch_shorts.fileutil import file_lock\n"
        "with file_lock(%r):\n    print('locked', flush=True); time.sleep(30)") % (
        str(Path(__file__).resolve().parents[1]), str(tmp_path / "work" / "auto"))], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert ctl.start() is False and "別の auto" in ctl.error
        ctl.stop()  # 別の auto にも停止のファイルで止まるよう伝える
        assert ctl.stop_file.exists()
    finally:
        holder.kill()
    with file_lock(tmp_path / "work" / "auto", timeout=5):
        pass


def test_log_file_keeps_console_output(tmp_path):
    import logging

    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    console = logging.StreamHandler()
    root.handlers[:] = [console]
    try:
        tray._setup_logging(tmp_path / "logs" / "tray.log")
        tray._setup_logging(tmp_path / "logs" / "tray.log")  # 2 回呼んでも同じファイルに二重に書かない
        assert console in root.handlers
        files = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert len(files) == 1
        logging.getLogger("twitch_shorts").info("こんにちは")
        files[0].flush()
        assert "こんにちは" in (tmp_path / "logs" / "tray.log").read_text(encoding="utf-8")
    finally:
        for h in root.handlers:
            if isinstance(h, logging.FileHandler):
                h.close()
        root.handlers[:], root.level = saved
