"""タスクトレイ (通知領域) に常駐し、アイコンのメニューから自動処理 (auto) を切り替える。

    twitch-shorts tray yuuki_ftw

- 「自動処理」: auto を裏で動かす / 止める。止めるときは処理の途中でも成果物が壊れないよう、
  いまの処理が終わってから止まる (Ctrl+C と同じ)
- 「PC の起動時に起動」: Windows のスタートアップにこのアイコンを登録する / 外す
- 「編集画面を開く」: 一番新しいショートをタイムラインで直す画面 (twitch-shorts edit) をブラウザで開く
- 「表示言語 / Language」: メニューと編集画面の言語 (日本語 / English) を切り替える
- アイコンの色: 緑 = 動作中、黄 = 止めている途中、灰 = 停止、赤 = エラーで停止
pip install 'twitch-shorts[tray]' (pystray と Pillow) が必要。
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .fileutil import file_lock, read_json, write_json_atomic
from .i18n import LANGUAGES, set_ui_language, tray_text, ui_language
from .watcher import STOP_FILE_ENV

log = logging.getLogger(__name__)

STARTUP_NAME = "twitch-shorts-tray.lnk"
OLD_STARTUP_NAME = "twitch-shorts-auto.lnk"  # toggle-startup.bat / register-startup.bat で作ったもの
COLORS = {"running": (46, 160, 67), "stopping": (212, 167, 44), "stopped": (110, 118, 129), "error": (207, 34, 46)}
CREATE_NO_WINDOW = 0x08000000


class AutoController:
    """auto を子プロセスとして動かし、止める。アイコンの表示とは切り離してある (テストしやすいように)。"""

    def __init__(self, channel: str, work_dir: str | Path, config_paths: list[str] | None = None,
                 cwd: str | Path | None = None, popen=subprocess.Popen):
        self.channel = channel
        self.work_dir = Path(work_dir)
        self.config_paths = [str(Path(p).resolve()) for p in config_paths or []]
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.popen = popen
        self.proc = None
        self.stopping = False
        self.error = ""
        self.log_path: Path | None = None
        self._log_file = None
        self._lock = threading.Lock()

    # --- 状態 ---------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.work_dir / "tray.json"

    @property
    def stop_file(self) -> Path:
        return self.work_dir / "tray.stop"

    def auto_enabled(self) -> bool:
        data = read_json(self.state_path, {})
        return bool(data.get("auto", True)) if isinstance(data, dict) else True

    def save_auto_enabled(self, enabled: bool) -> None:
        write_json_atomic(self.state_path, {"auto": enabled})

    def running(self) -> bool:
        proc = self.proc  # 別のスレッドが None にしても大丈夫なように、一度だけ読む
        return proc is not None and proc.poll() is None

    def auto_running_elsewhere(self) -> bool:
        """このアイコン以外で auto が動いているか (前のアイコンが強制終了されて残った auto、手動で起動した auto)。"""
        probe = file_lock(self.work_dir / "auto", timeout=0.2)
        try:
            probe.__enter__()
        except TimeoutError:
            return True
        except OSError:
            return False
        probe.__exit__(None, None, None)
        return False

    def status(self) -> str:
        if self.running():
            return "stopping" if self.stopping else "running"
        return "error" if self.error else "stopped"

    def status_text(self, lang: str = "ja") -> str:
        return tray_text(lang, self.status(), error=self.error)

    # --- 操作 ---------------------------------------------------------------

    def command(self, *args: str) -> list[str]:
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and python.with_name("python.exe").exists():
            python = python.with_name("python.exe")  # 出力をログに残すため (窓は CREATE_NO_WINDOW で出さない)
        cmd = [str(python), "-m", "twitch_shorts.cli"]
        for p in self.config_paths:
            cmd += ["-c", p]
        return cmd + (list(args) or ["auto", self.channel])

    def start(self) -> bool:
        with self._lock:
            if self.running():
                return False
            self.work_dir.mkdir(parents=True, exist_ok=True)
            if self.auto_running_elsewhere():
                self.error = "別の auto がすでに動いています。止めるには自動処理を OFF にしてください"
                return False
            self.stop_file.unlink(missing_ok=True)
            logs = self.work_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            self.log_path = logs / f"auto_{datetime.now():%Y%m%d_%H%M%S}.log"
            self._close_log()
            env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", STOP_FILE_ENV: str(self.stop_file)}
            kwargs = {"creationflags": CREATE_NO_WINDOW} if os.name == "nt" else {}
            try:
                self._log_file = open(self.log_path, "ab")
                self.proc = self.popen(self.command(), cwd=self.cwd, env=env, stdout=self._log_file,
                                       stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
            except OSError as e:
                self._close_log()
                self.error = f"起動できませんでした: {e}"
                return False
            self.stopping = False
            self.error = ""
            log.info("auto を起動しました (ログ: %s)", self.log_path)
            return True

    def stop(self) -> None:
        """止める指示を出す (いまの処理が終わってから止まる)。

        このアイコン以外で動いている auto (前のアイコンが強制終了されて残ったものなど) も、同じ停止のファイルで止まる。
        """
        with self._lock:
            mine = self.running()
            if mine or self.auto_running_elsewhere():
                self.stop_file.parent.mkdir(parents=True, exist_ok=True)
                self.stop_file.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")
                self.stopping = mine
            if not mine:
                self.error = ""

    def kill(self) -> None:
        """すぐに止める。録画ツールや ffmpeg など、auto が起動したものもまとめて止める。"""
        with self._lock:
            proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                               creationflags=CREATE_NO_WINDOW)
            proc.kill()

    def set_auto(self, enabled: bool) -> None:
        """自動処理の ON / OFF。止めている途中に ON に戻した場合は、止まったあとで起動し直す (keep_alive)。"""
        self.save_auto_enabled(enabled)
        if enabled:
            self.error = ""
            self.start()
        else:
            self.stop()

    def keep_alive(self) -> bool:
        """ON なのに動いていなければ起動する (止めている途中に ON に戻した場合など)。エラーで止まったときは起動しない。"""
        if self.auto_enabled() and not self.error and not self.running():
            return self.start()
        return False

    def check(self) -> bool:
        """子プロセスの終了に気付いたら後片付けをする。状態が変わったら True。"""
        with self._lock:
            if self.proc is None or self.proc.poll() is None:
                return False
            code = self.proc.returncode
            if not self.stopping and code not in (0, 130):
                self.error = f"終了コード {code}"
            self.proc = None
            self.stopping = False
            self.stop_file.unlink(missing_ok=True)
            self._close_log()
            return True

    def _close_log(self) -> None:
        if self._log_file:
            self._log_file.close()
            self._log_file = None


# ---------------------------------------------------------------------------
# PC の起動時に起動 (Windows のスタートアップ)
# ---------------------------------------------------------------------------

def _startup_dir() -> Path | None:
    if os.name != "nt":
        return None
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" if appdata else None


def startup_enabled() -> bool:
    d = _startup_dir()
    return bool(d and (d / STARTUP_NAME).exists())


def set_startup(enabled: bool, channel: str, config_paths: list[str] | None = None,
                cwd: str | Path | None = None, run=subprocess.run) -> None:
    """スタートアップにこのアイコンのショートカットを作る / 消す。"""
    d = _startup_dir()
    if d is None:
        raise RuntimeError("PC の起動時に起動する設定は Windows だけで使えます")
    link = d / STARTUP_NAME
    if not enabled:
        link.unlink(missing_ok=True)
        return
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else python  # pythonw なら黒い窓が出ない
    args = ["-m", "twitch_shorts.cli"]
    for p in config_paths or []:
        args += ["-c", str(Path(p).resolve())]
    args += ["tray", channel]
    env = {**os.environ, "TS_LINK": str(link), "TS_TARGET": str(target),
           "TS_ARGS": subprocess.list2cmdline(args), "TS_CWD": str(Path(cwd or os.getcwd()).resolve())}
    # パスは環境変数で渡す (日本語のフォルダー名や & ( ) を含んでもそのまま使えるように)
    script = ("$ErrorActionPreference = 'Stop'; $s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:TS_LINK); "
              "$s.TargetPath = $env:TS_TARGET; $s.Arguments = $env:TS_ARGS; $s.WorkingDirectory = $env:TS_CWD; $s.Save()")
    run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], env=env, check=True,
        capture_output=True, creationflags=CREATE_NO_WINDOW)
    # 以前の .bat で登録した auto の自動起動が残っていると二重に動くので外す
    (d / OLD_STARTUP_NAME).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# アイコン
# ---------------------------------------------------------------------------

def _config_language(config_paths: list[str] | None) -> str:
    """設定ファイルの [ui] language (画面で切り替えていないときの言語)。"""
    try:
        from .config import load_config

        return load_config(config_paths or None).ui.language
    except Exception:  # 設定の誤りは auto 側で知らせる
        return "ja"


def _setup_logging(path: Path) -> None:
    """pythonw で起動したときは画面に何も出ないので、ログをファイルに残す。"""
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) and h.baseFilename == str(path.resolve()) for h in root.handlers):
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    # 起動時 (cli) の画面への出力は残したまま、ファイルにも書く (pythonw では画面が無いので画面への出力は外す)
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) and h.stream is None:
            root.removeHandler(h)
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)


def _alert(message: str) -> None:
    """エラーを伝える。pythonw で起動していて画面に出せないときは、Windows のメッセージボックスで出す。"""
    log.error(message)
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    elif os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, "twitch-shorts", 0x30)


def make_image(status: str):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), radius=14, fill=COLORS[status] + (255,))
    # 縦長の画面 (ショート) の形
    d.rounded_rectangle((22, 12, 42, 52), radius=4, outline=(255, 255, 255, 255), width=4)
    if status == "running":
        d.polygon([(29, 25), (29, 39), (38, 32)], fill=(255, 255, 255, 255))
    return img


def _windows_browser() -> str | None:
    """既定のブラウザ (https の関連付け) の exe。.html が VS Code などに関連付けられていても使えるように。"""
    try:
        import winreg

        key = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            prog_id = winreg.QueryValueEx(k, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as k:
            command = winreg.QueryValueEx(k, "")[0]
    except OSError:
        return None
    m = re.match(r'\s*"([^"]+)"', command) or re.match(r"\s*(\S+)", command)
    return m.group(1) if m else None


def open_path(path: Path) -> None:
    if os.name == "nt":
        browser = _windows_browser() if path.suffix.lower() in (".html", ".htm") else None
        if browser:
            subprocess.Popen([browser, path.resolve().as_uri()])
            return
        os.startfile(str(path))  # noqa: S606 - 自分の出力フォルダー・ファイルを開くだけ
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def latest_review_page(output_dir: str | Path) -> Path | None:
    pages = [p for p in Path(output_dir).glob("**/index.html") if p.is_file()]
    return max(pages, key=lambda p: p.stat().st_mtime) if pages else None


def run_tray(channel: str, work_dir: str, output_dir: str, config_paths: list[str] | None = None) -> int:
    work_dir = str(Path(work_dir).resolve())
    output_dir = str(Path(output_dir).resolve())
    _setup_logging(Path(work_dir) / "logs" / "tray.log")
    try:
        import pystray
        from PIL import Image  # noqa: F401
    except ImportError as e:
        if getattr(e, "name", "") in ("pystray", "PIL"):
            _alert("タスクトレイのアイコンには pystray と Pillow が必要です: pip install -e \".[tray]\"")
        else:
            _alert(f"タスクトレイを使えません: {e}")
        return 1
    except Exception as e:  # 画面の無い環境など
        _alert(f"タスクトレイを使えません: {e}")
        return 1

    # 2 つ目のアイコンは起動しない (auto が二重に動かないように)
    single = file_lock(Path(work_dir) / "tray", timeout=0.5)
    try:
        single.__enter__()
    except TimeoutError:
        _alert("タスクトレイのアイコンはもう起動しています (画面右下の ^ の中を確認してください)")
        return 1
    except OSError as e:
        _alert(f"タスクトレイのアイコンを起動できません: {e}")
        return 1

    ctl = AutoController(channel, work_dir, config_paths)
    quitting = threading.Event()
    default_lang = _config_language(config_paths)
    editor_proc: list = []

    def lang() -> str:
        return ui_language(work_dir, default_lang)

    def text(key: str):
        return lambda item: tray_text(lang(), key)

    def notify(icon, message: str) -> None:
        try:
            icon.notify(message, "twitch-shorts")
        except Exception:  # 通知に対応していない環境
            log.warning(message)

    def refresh(icon) -> None:
        status = ctl.status()
        icon.icon = make_image(status)
        icon.title = f"twitch-shorts ({channel}): {ctl.status_text(lang())}"[:127]
        icon.update_menu()

    def in_background(fn):
        # メニューの操作は裏で行う (PowerShell の起動などでアイコンが固まらないように)
        def action(icon, item) -> None:
            def run() -> None:
                try:
                    fn(icon)
                except Exception as e:
                    log.exception("メニューの操作に失敗しました")
                    notify(icon, f"失敗しました: {e}")
                refresh(icon)
            threading.Thread(target=run, daemon=True).start()
        return action

    def auto_checked(item) -> bool:
        # 保存した設定ではなく実際の状態を表示する (エラーで止まったときは外れて見える)
        return ctl.status() == "running"

    @in_background
    def toggle_auto(icon) -> None:
        ctl.set_auto(ctl.status() != "running")
        if ctl.error:
            notify(icon, ctl.error)

    @in_background
    def toggle_startup(icon) -> None:
        set_startup(not startup_enabled(), channel, config_paths)

    def open_output(icon, item) -> None:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        open_path(Path(output_dir).resolve())

    def open_review(icon, item) -> None:
        page = latest_review_page(output_dir)
        if page:
            open_path(page)
        else:
            notify(icon, "まだ確認ページがありません。配信が終わると作られます" if lang() == "ja"
                   else "There is no review page yet. It is made after a stream ends")

    def open_editor(icon, item) -> None:
        from .editor import find_projects

        found = find_projects(output_dir)
        if not found:
            notify(icon, tray_text(lang(), "no_project"))
            return
        for proc in editor_proc:  # 前に開いた編集画面のサーバーは閉じる (1 つだけにする)
            if proc.poll() is None:
                proc.terminate()
        editor_proc.clear()
        log_dir = Path(work_dir) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "editor.log", "a", encoding="utf-8") as out:
            editor_proc.append(ctl.popen(ctl.command("edit", str(found[0])), cwd=ctl.cwd, stdout=out,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0))

    def choose_language(code: str):
        def action(icon, item) -> None:
            set_ui_language(work_dir, code)
            refresh(icon)
        return action

    def open_log(icon, item) -> None:
        if ctl.log_path and ctl.log_path.exists():
            open_path(ctl.log_path)
        else:
            open_path((Path(work_dir) / "logs").resolve())

    exit_timers: list[threading.Timer] = []

    def close(icon) -> None:
        log.info("アイコンを閉じます")
        icon.stop()
        # ponytail: 実機で終了を押してもメッセージループが終わらず残ることがあった (テストでは再現せず原因不明)。
        # auto はもう止まっているので、5 秒で閉じなければプロセスごと終える
        # アイコンが普通に閉じたら止める (後片付けの途中で終わらせない)
        timer = threading.Timer(5, os._exit, args=(0,))
        timer.daemon = True
        exit_timers.append(timer)
        timer.start()

    def quit_app(icon, item) -> None:
        log.info("終了が押されました")
        quitting.set()  # 自動処理の ON / OFF はそのまま (次に起動したときも同じ状態で始まる)
        if ctl.running():
            ctl.stop()  # 処理の途中なら、終わってからアイコンを閉じる (監視のスレッドが閉じる)
        refresh(icon)
        if not ctl.running():
            close(icon)

    @in_background
    def kill_now(icon) -> None:
        ctl.kill()

    languages = pystray.Menu(*[
        pystray.MenuItem(name, choose_language(code), checked=(lambda item, c=code: lang() == c), radio=True)
        for code, name in LANGUAGES.items()
    ])
    menu = pystray.Menu(
        pystray.MenuItem(lambda item: ctl.status_text(lang()), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(text("auto"), toggle_auto,
                         checked=auto_checked, enabled=lambda item: not quitting.is_set()),
        pystray.MenuItem(text("startup"), toggle_startup,
                         checked=lambda item: startup_enabled(), visible=os.name == "nt"),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(text("review"), open_review, default=True),
        pystray.MenuItem(text("editor"), open_editor),
        pystray.MenuItem(text("output"), open_output),
        pystray.MenuItem(text("log"), open_log),
        pystray.MenuItem(text("language"), languages),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(text("kill"), kill_now, visible=lambda item: ctl.stopping),
        pystray.MenuItem(text("quit"), quit_app),
    )
    icon = pystray.Icon("twitch-shorts", make_image("stopped"), f"twitch-shorts ({channel})", menu)

    def watch_child(icon) -> None:
        icon.visible = True
        first = True
        while True:
            try:
                if not first:
                    time.sleep(2)
                changed = ctl.check()
                if changed and ctl.error and not quitting.is_set():
                    notify(icon, tray_text(lang(), "stopped_notify", error=ctl.error))
                if not quitting.is_set() and ctl.keep_alive():
                    changed = True
                if changed or first:
                    refresh(icon)
                first = False
                if quitting.is_set() and not ctl.running():
                    close(icon)
                    return
            except Exception:  # 1 回の失敗で監視をやめない
                log.exception("auto の様子を確認できませんでした")

    try:
        icon.run(setup=watch_child)
    finally:
        for t in exit_timers:
            t.cancel()
        for proc in editor_proc:
            if proc.poll() is None:
                proc.terminate()
        ctl.stop()
        single.__exit__(None, None, None)
    return 0
