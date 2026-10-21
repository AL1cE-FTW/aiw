"""表示言語 (日本語 / English) の切り替え。タスクトレイのメニューと編集画面で使う。

選んだ言語は work/ui.json に保存し、次に起動したときも同じ言語で始まる。
"""

from __future__ import annotations

from pathlib import Path

from .fileutil import read_json, write_json_atomic

LANGUAGES = {"ja": "日本語", "en": "English"}
UI_FILE = "ui.json"

TRAY_TEXT = {
    "ja": {
        "running": "動作中 (配信を待って自動でショートを作ります)",
        "stopping": "止めています… (いまの処理が終わったら止まります)",
        "stopped": "停止中",
        "error": "エラーで停止しました ({error})。ログを確認してください",
        "auto": "自動処理 (配信ごとにショートを作る)",
        "startup": "PC の起動時にこのアイコンを起動",
        "make": "配信を選んで作る (✓ は作成済み)",
        "loading": "配信の一覧を読み込み中… (少し待って開き直してください)",
        "busy": "いま作っているところです。終わってからもう一度選んでください",
        "making": "作っています (数十分かかります): {title}",
        "done": "作り終わりました。確認ページを開いてください",
        "in_use": "この配信は別の処理で作成中です。終わってからもう一度選んでください",
        "failed": "作れませんでした。ログを確認してください: {log}",
        "quit_wait": "配信から作っている途中です。作り終わってから閉じます",
        "review": "最新の確認ページを開く",
        "editor": "編集画面を開く (タイムライン)",
        "output": "出力フォルダーを開く",
        "log": "ログを開く",
        "language": "表示言語 / Language",
        "kill": "すぐに止める (作りかけは残りません)",
        "quit": "終了",
        "no_project": "編集できるショートがまだありません",
        "stopped_notify": "自動処理が止まりました ({error})。ログを確認してください",
    },
    "en": {
        "running": "Running (makes shorts automatically after each stream)",
        "stopping": "Stopping… (stops after the current job)",
        "stopped": "Stopped",
        "error": "Stopped with an error ({error}). Check the log",
        "auto": "Auto mode (make shorts for every stream)",
        "startup": "Start this icon when the PC starts",
        "make": "Make from a stream (✓ = already made)",
        "loading": "Loading streams… (reopen the menu in a moment)",
        "busy": "Already making one. Choose again after it finishes",
        "making": "Making (takes tens of minutes): {title}",
        "done": "Done. Open the review page",
        "in_use": "This stream is being made by another job. Try again after it finishes",
        "failed": "Failed. Check the log: {log}",
        "quit_wait": "Still making from a stream. Quits when it finishes",
        "review": "Open the latest review page",
        "editor": "Open the editor (timeline)",
        "output": "Open the output folder",
        "log": "Open the log",
        "language": "表示言語 / Language",
        "kill": "Stop now (discards the current job)",
        "quit": "Quit",
        "no_project": "There are no shorts to edit yet",
        "stopped_notify": "Auto mode stopped ({error}). Check the log",
    },
}


def normalize_language(value) -> str:
    lang = str(value or "").strip().lower()[:2]
    return lang if lang in LANGUAGES else "ja"


def ui_language(work_dir: str | Path, default: str = "ja") -> str:
    data = read_json(Path(work_dir) / UI_FILE, {})
    return normalize_language(data.get("language") if isinstance(data, dict) and data.get("language") else default)


def set_ui_language(work_dir: str | Path, lang: str) -> str:
    lang = normalize_language(lang)
    path = Path(work_dir) / UI_FILE
    data = read_json(path, {})
    data = data if isinstance(data, dict) else {}
    data["language"] = lang
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, data)
    return lang


def tray_text(lang: str, key: str, **kw) -> str:
    return TRAY_TEXT[normalize_language(lang)][key].format(**kw)
