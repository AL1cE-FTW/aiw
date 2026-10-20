"""編集画面 (twitch-shorts edit) のテスト。サーバーを実際に立てて、ブラウザの代わりに HTTP で操作する。"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from twitch_shorts.audio import probe_duration
from twitch_shorts.chat import load_chat
from twitch_shorts.config import Config
from twitch_shorts.editor import (EditError, Editor, clean_edit, cuts_from_keep, find_projects, keep_from_cuts,
                                  make_handler)
from twitch_shorts.i18n import set_ui_language, tray_text, ui_language
from twitch_shorts.pipeline import LocalSource, process


def test_cuts_and_keep_round_trip():
    keep = keep_from_cuts(10, 40, [[15, 18], [17, 20], [38, 50], [0, 5]])
    assert keep == [[10, 15], [20, 38]]
    assert cuts_from_keep(10, 40, keep) == [[15, 20], [38, 40]]
    assert keep_from_cuts(10, 40, []) == [] and cuts_from_keep(10, 40, []) == []


def test_clean_edit_rejects_bad_values():
    ok = clean_edit({"start": 10, "end": 30, "title": " t ", "segments": [[12, 14, " やあ "], [15, 15, "x"]],
                     "cuts": [[20, 22]], "comments": 1}, duration=100)
    assert ok["title"] == "t" and ok["segments"] == [[12, 14, "やあ"]] and ok["cuts"] == [[20, 22]] and ok["comments"]
    for bad in ({"start": 10, "end": 10.5}, {"start": "x", "end": 20}, {"start": 90, "end": 120},
                {"start": 0, "end": 300}, {"start": 10, "end": 20, "segments": [["a"]]},
                {"start": 10, "end": 20, "cuts": [[10, 19.5]]}, {"start": float("nan"), "end": 20}):
        with pytest.raises(EditError):
            clean_edit(bad, duration=100)


def test_language_setting(tmp_path):
    assert ui_language(tmp_path) == "ja" and ui_language(tmp_path, "en") == "en"
    assert set_ui_language(tmp_path, "EN") == "en" and ui_language(tmp_path) == "en"
    assert set_ui_language(tmp_path, "fr") == "ja"
    assert tray_text("en", "error", error="x") == "Stopped with an error (x). Check the log"


@pytest.fixture
def run(make_stream, tmp_path):
    video, chat_path = make_stream(duration=90, events=(40,))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    cfg.render.title_font_size, cfg.render.subtitle_font_size, cfg.render.comment_font_size = 30, 24, 20
    cfg.detect.top_n = 1
    run_dir = tmp_path / "out" / "yuuki_ftw" / "20261020_200000"
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), run_dir, channel="yuuki_ftw")
    assert r.highlights[0].output_path
    return cfg, run_dir


@pytest.fixture
def server(run):
    cfg, run_dir = run
    editor = Editor(cfg, run_dir)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(editor))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, body=None, token=editor.token, headers=None):
        req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Token": token, "Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                return res.status, res.read(), dict(res.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    yield editor, call, run_dir, cfg
    httpd.shutdown()
    httpd.server_close()


def test_project_is_written_and_found(run):
    cfg, run_dir = run
    project = json.loads((run_dir / "project.json").read_text(encoding="utf-8"))
    assert project["source"]["type"] == "local" and len(project["shorts"]) == 1
    assert project["shorts"][0]["chat"]  # 区間の前後のチャットも残す
    assert find_projects(cfg.output_dir) == [run_dir.resolve()] or find_projects(cfg.output_dir) == [run_dir]


def test_editor_rejects_other_sites(server):
    editor, call, *_ = server
    assert call("/api/state", token="wrong")[0] == 403
    assert call("/api/state", headers={"Host": "evil.example"})[0] == 403
    status, body, _ = call(f"/?t={editor.token}", token="")
    assert status == 200 and b"Shorts Editor" in body and editor.token.encode() in body


def test_edit_and_render_again(server):
    editor, call, run_dir, cfg = server
    status, body, _ = call("/api/state")
    state = json.loads(body)
    assert status == 200 and state["lang"] == "ja" and len(state["shorts"]) == 1
    edit = state["shorts"][0]["edit"]

    # 元の映像を範囲指定で読める (シークのため)
    src = json.loads(call("/api/source?i=0")[1])
    status, chunk, headers = call(src["url"], headers={"Range": "bytes=0-99"})
    assert status == 206 and len(chunk) == 100 and headers["Content-Range"].startswith("bytes 0-99/")
    assert call("/media/nothing")[0] == 404

    # 区間・タイトル・字幕・カット・コメントを変えて書き出し直す
    edit.update(start=30.0, end=50.0, title="直したタイトル", hook="", comments=True,
                segments=[[32, 35, "直した字幕"]], cuts=[[40, 44]])
    assert call("/api/save", {"i": 5, "edit": edit})[0] == 400
    assert call("/api/save", {"i": 0, "edit": {**edit, "end": 30.2}})[0] == 400
    assert call("/api/save", {"i": 0, "edit": edit})[0] == 200
    editor.render(0, edit, wait=True)
    assert editor.jobs[0]["status"] == "done", editor.jobs[0]
    project = json.loads((run_dir / "project.json").read_text(encoding="utf-8"))
    h = project["shorts"][0]["highlight"]
    assert (h["start"], h["end"], h["title"]) == (30.0, 50.0, "直したタイトル")
    assert h["keep"] == [[30.0, 40.0], [44.0, 50.0]]
    # 先見せ 2 秒 + 20 秒 - カット 4 秒
    assert probe_duration(h["output_path"]) == pytest.approx(18, abs=0.4)
    report = json.loads((run_dir / "highlights.json").read_text(encoding="utf-8"))
    assert report["highlights"][0]["title"] == "直したタイトル"
    assert not list(run_dir.glob("*.editing.*"))
    # 開き直すと、保存した編集の続きから
    again = json.loads(call("/api/state")[1])["shorts"][0]["edit"]
    assert again["cuts"] == [[40.0, 44.0]] and again["segments"] == [[32.0, 35.0, "直した字幕"]]


def test_silence_detection_and_language(server):
    editor, call, run_dir, cfg = server
    edit = json.loads(call("/api/state")[1])["shorts"][0]["edit"]
    status, body, _ = call("/api/silence", {"i": 0, "edit": edit})
    assert status == 200 and isinstance(json.loads(body)["cuts"], list)
    assert json.loads(call("/api/lang", {"lang": "en"})[1]) == {"lang": "en"}
    assert json.loads(call("/api/state")[1])["lang"] == "en"
    assert ui_language(cfg.work_dir) == "en"


def test_failed_render_keeps_the_old_short(server, monkeypatch):
    editor, call, run_dir, cfg = server
    before = json.loads((run_dir / "project.json").read_text(encoding="utf-8"))["shorts"][0]["highlight"]
    old = open(before["output_path"], "rb").read()

    def boom(*a, **k):
        raise RuntimeError("ffmpeg が失敗しました")

    monkeypatch.setattr("twitch_shorts.render.render_highlight", boom)
    edit = editor.current_edit(0)
    editor.render(0, edit, wait=True)
    assert editor.jobs[0] == {"status": "error", "message": "ffmpeg が失敗しました"}
    assert open(before["output_path"], "rb").read() == old


def test_edit_command_without_projects(tmp_path, capsys):
    from twitch_shorts import cli

    assert cli.main(["edit", "-o", str(tmp_path / "none"), "--no-browser"]) == 1
    assert "編集できるショートがありません" in capsys.readouterr().err
