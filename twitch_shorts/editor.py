"""書き出したショートを直す編集画面 (タイムライン)。``twitch-shorts edit`` でブラウザに開く。

PC の中だけで動く小さな Web サーバー (127.0.0.1) を立て、ブラウザで次のことができる:
  - 元の映像を見ながら、区間の始まり・終わりをドラッグで直す
  - 字幕 (文字起こし) の文字と時刻を直す・足す・消す
  - 無音カットの位置を直す (無音を検出し直す・ドラッグで足す・クリックで消す)
  - タイトル・引きの言葉・コメント字幕の有無を変える
  - 元に戻す / やり直す (Ctrl+Z / Ctrl+Shift+Z)、保存、書き出し直し
  - 表示言語の切り替え (日本語 / English)

書き出しは自動処理と同じ仕組み (render_highlight) で行うので、仕上がりは自動で作ったものと揃う。
編集の内容は run_dir/project.json に保存され、次に開いたときも続きから直せる。
"""

from __future__ import annotations

import copy
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config
from .fileutil import read_json, write_json_atomic
from .i18n import LANGUAGES, set_ui_language, ui_language
from .models import ChatMessage, Highlight, TranscriptSegment
from .pipeline import PROJECT_FILE, source_from_dict

log = logging.getLogger(__name__)

# 元の映像を区間の前後どれだけ多めに用意するか (区間を広げて確かめられるように)
SOURCE_MARGIN = 20.0


class EditError(ValueError):
    pass


def find_projects(output_dir: str | Path) -> list[Path]:
    """編集できる実行 (project.json がある出力フォルダー) を新しい順に返す。配信中のプレビューは除く。"""
    out = Path(output_dir)
    found = [p.parent for p in out.rglob(PROJECT_FILE) if "live" not in p.parent.relative_to(out).parts[1:]]
    return sorted(found, key=lambda d: (d / PROJECT_FILE).stat().st_mtime, reverse=True)


def keep_from_cuts(start: float, end: float, cuts: list) -> list[list[float]]:
    """カット (詰める区間) の一覧を、残す区間の一覧にする。カットが無ければ空 (= 全部使う)。"""
    spans = sorted((max(start, float(a)), min(end, float(b))) for a, b in cuts or [])
    spans = [(a, b) for a, b in spans if b - a >= 0.05]
    if not spans:
        return []
    keep, t = [], start
    for a, b in spans:
        if a > t:
            keep.append([round(t, 2), round(a, 2)])
        t = max(t, b)
    if t < end:
        keep.append([round(t, 2), round(end, 2)])
    return keep


def cuts_from_keep(start: float, end: float, keep: list) -> list[list[float]]:
    if not keep:
        return []
    cuts, t = [], start
    for a, b in sorted(keep):
        if a > t + 0.01:
            cuts.append([round(t, 2), round(a, 2)])
        t = max(t, b)
    if t < end - 0.01:
        cuts.append([round(t, 2), round(end, 2)])
    return cuts


def _num(value, name: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise EditError(f"{name} が数ではありません: {value!r}") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise EditError(f"{name} が数ではありません: {value!r}")
    return v


def clean_edit(edit: dict, duration: float | None) -> dict:
    """ブラウザから届いた編集内容を確かめて整える (おかしな値は EditError)。"""
    if not isinstance(edit, dict):
        raise EditError("編集内容の形式が違います")
    start, end = _num(edit.get("start"), "start"), _num(edit.get("end"), "end")
    if start < 0 or end - start < 1 or (duration and end > duration + 1):
        raise EditError("区間は 1 秒以上で、動画の範囲内にしてください")
    if end - start > 180:
        raise EditError("区間は 180 秒までにしてください")
    segments = []
    for s in edit.get("segments") or []:
        if not isinstance(s, (list, tuple)) or len(s) != 3:
            raise EditError("字幕の形式が違います")
        a, b, text = _num(s[0], "字幕の開始"), _num(s[1], "字幕の終了"), str(s[2]).strip()
        if b > a and text:
            segments.append([round(a, 2), round(b, 2), text[:200]])
    cuts = []
    for c in edit.get("cuts") or []:
        if not isinstance(c, (list, tuple)) or len(c) != 2:
            raise EditError("カットの形式が違います")
        a, b = _num(c[0], "カットの開始"), _num(c[1], "カットの終了")
        if b > a:
            cuts.append([round(a, 2), round(b, 2)])
    keep = keep_from_cuts(start, end, cuts)
    if keep and sum(b - a for a, b in keep) < 1:
        raise EditError("カットで区間がほとんど残りません")
    return {
        "start": round(start, 2), "end": round(end, 2),
        "title": str(edit.get("title") or "").strip()[:200],
        "hook": str(edit.get("hook") or "").strip()[:100],
        "segments": sorted(segments),
        "cuts": cuts_from_keep(start, end, keep),
        "comments": bool(edit.get("comments")),
    }


class Editor:
    """1 回分の実行 (run_dir) の編集。サーバーのスレッドから呼ばれるので、変更はロックの中で行う。"""

    def __init__(self, cfg: Config, run_dir: str | Path):
        self.cfg = cfg
        self.run_dir = Path(run_dir).resolve()
        self.path = self.run_dir / PROJECT_FILE
        project = read_json(self.path, None)
        if not isinstance(project, dict) or not project.get("shorts"):
            raise FileNotFoundError(f"編集できるショートがありません: {self.path}")
        self.project = project
        self.source = source_from_dict(project.get("source") or {})
        self.token = secrets.token_urlsafe(18)
        self.lock = threading.RLock()
        self.media: dict[str, Path] = {}
        self.jobs: dict[int, dict] = {}

    # --- 読み出し ---------------------------------------------------------------

    def short(self, i: int) -> dict:
        shorts = self.project["shorts"]
        if not isinstance(i, int) or not 0 <= i < len(shorts):
            raise EditError("ショートの番号が違います")
        return shorts[i]

    def current_edit(self, i: int) -> dict:
        s = self.short(i)
        if isinstance(s.get("edit"), dict):
            return s["edit"]
        h = s["highlight"]
        return {
            "start": h["start"], "end": h["end"], "title": h.get("title", ""), "hook": h.get("hook", ""),
            "segments": [list(x) for x in s.get("segments") or []],
            "cuts": cuts_from_keep(h["start"], h["end"], h.get("keep") or []),
            "comments": bool(self.cfg.render.comments),
        }

    def _media_url(self, key: str, path: Path) -> str:
        self.media[key] = Path(path).resolve()
        try:
            v = int(Path(path).stat().st_mtime)
        except OSError:
            v = 0
        return f"/media/{key}?t={self.token}&v={v}"

    def state(self) -> dict:
        with self.lock:
            shorts = []
            for i, s in enumerate(self.project["shorts"]):
                h = s["highlight"]
                out = Path(h.get("output_path") or "")
                shorts.append({
                    "index": i, "peak": h.get("peak", h["start"]), "score": h.get("score", 0),
                    "output": self._media_url(f"out{i}", out) if out.is_file() else "",
                    "video_duration": h.get("video_duration", 0),
                    "edit": self.current_edit(i), "saved": isinstance(s.get("edit"), dict),
                    "chat": s.get("chat") or [],
                })
            return {
                "channel": self.project.get("channel", ""), "stream_title": self.project.get("stream_title", ""),
                "duration": self.project.get("duration"), "shorts": shorts, "jobs": self.jobs,
                "lang": ui_language(self.cfg.work_dir, self.cfg.ui.language), "languages": LANGUAGES,
                "render": {"width": self.cfg.render.width, "height": self.cfg.render.height,
                           "comment_seconds": self.cfg.render.comment_seconds,
                           "comment_lines": self.cfg.render.comment_lines},
            }

    def source_media(self, i: int) -> dict:
        """区間の前後も含めた元の映像を用意する (VOD なら区間をダウンロードする)。"""
        e = self.current_edit(i)
        duration = self.project.get("duration") or None
        a = max(0.0, e["start"] - SOURCE_MARGIN)
        b = e["end"] + SOURCE_MARGIN
        if duration:
            b = min(float(duration), b)
        video, offset = self.source.video_for(a, b)
        with self.lock:
            return {"url": self._media_url(f"src{i}", Path(video)), "offset": offset}

    def detect_silence(self, i: int, start: float, end: float) -> list[list[float]]:
        from .audio import silent_ranges
        from .render import keep_ranges

        r = self.cfg.render
        video, offset = self.source.video_for(start, end)
        silences = silent_ranges(video, start - offset, end - start, r.silence_db, r.silence_min)
        keep = keep_ranges(silences, end - start, r.silence_pad)
        return cuts_from_keep(start, end, [[start + a, start + b] for a, b in keep])

    # --- 保存・書き出し ---------------------------------------------------------

    def save(self, i: int, edit: dict) -> dict:
        edit = clean_edit(edit, self.project.get("duration"))
        with self.lock:
            self.short(i)["edit"] = edit
            write_json_atomic(self.path, self.project)
        return edit

    def render(self, i: int, edit: dict, wait: bool = False) -> None:
        edit = self.save(i, edit)
        with self.lock:
            if self.jobs.get(i, {}).get("status") == "rendering":
                raise EditError("このショートは書き出し中です")
            self.jobs[i] = {"status": "rendering", "message": ""}
        t = threading.Thread(target=self._render, args=(i, edit), daemon=True)
        t.start()
        if wait:
            t.join()

    def _render(self, i: int, edit: dict) -> None:
        from .render import render_highlight

        try:
            with self.lock:
                s = copy.deepcopy(self.short(i))
            h = Highlight.from_dict(s["highlight"])
            h.start, h.end, h.title, h.hook = edit["start"], edit["end"], edit["title"], edit["hook"]
            if not h.start <= h.peak <= h.end:
                h.peak = (h.start + h.end) / 2
            out = Path(h.output_path)
            tmp = out.with_name(out.stem + ".editing.mp4")
            cfg = copy.deepcopy(self.cfg.render)
            cfg.comments = edit["comments"]
            segments = [TranscriptSegment(a, b, text) for a, b, text in edit["segments"]]
            chat = [ChatMessage(float(o), str(u), str(t)) for o, u, t in s.get("chat") or []]
            keep = keep_from_cuts(edit["start"], edit["end"], edit["cuts"])
            video, offset = self.source.video_for(h.start, h.end)
            render_highlight(video, str(tmp), h, cfg, segments, source_offset=offset, chat=chat,
                             keep=[tuple(k) for k in keep])
            os.replace(tmp, out)
            srt = tmp.with_suffix(".srt")
            if srt.exists():
                os.replace(srt, out.with_suffix(".srt"))
            h.output_path = str(out)
            with self.lock:
                self.short(i)["highlight"] = h.to_dict()
                self.short(i)["segments"] = edit["segments"]
                write_json_atomic(self.path, self.project)
                _update_report(self.run_dir, h)
                self.jobs[i] = {"status": "done", "message": ""}
            log.info("書き出し直しました: %s", out.name)
        except Exception as e:  # 失敗しても元のショートは残る (一時ファイルに書いてから置き換えるため)
            log.warning("書き出しに失敗しました: %s", e)
            with self.lock:
                self.jobs[i] = {"status": "error", "message": str(e)[-500:]}

    def set_language(self, lang: str) -> str:
        return set_ui_language(self.cfg.work_dir, lang)


def _update_report(run_dir: Path, h: Highlight) -> None:
    """確認ページ・投稿予定が使う highlights.json の同じショートを書き換える。"""
    path = run_dir / "highlights.json"
    data = read_json(path, None)
    if not isinstance(data, dict):
        return
    for i, x in enumerate(data.get("highlights") or []):
        if isinstance(x, dict) and x.get("output_path") == h.output_path:
            x.update({k: v for k, v in h.to_dict().items() if k in ("start", "end", "title", "hook", "keep",
                                                                     "video_duration", "duration")})
            write_json_atomic(path, data)
            return


# ---------------------------------------------------------------------------
# Web サーバー
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def make_handler(editor: Editor):
    class Handler(BaseHTTPRequestHandler):
        server_version = "twitch-shorts-editor"

        def log_message(self, fmt, *args):  # アクセスのたびに画面へ出さない
            log.debug(fmt, *args)

        # --- 共通 ---
        def _host_ok(self) -> bool:
            # 別のサイトが DNS を書き換えてこのサーバーに届く攻撃 (DNS rebinding) を防ぐ
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in ("127.0.0.1", "localhost", "::1")

        def _token_ok(self, query: dict) -> bool:
            given = self.headers.get("X-Token") or (query.get("t") or [""])[0]
            return secrets.compare_digest(given, editor.token)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _check(self, query: dict) -> bool:
            if not self._host_ok():
                self._error(HTTPStatus.FORBIDDEN, "bad host")
                return False
            if not self._token_ok(query):
                self._error(HTTPStatus.FORBIDDEN, "bad token")
                return False
            return True

        # --- GET ---
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
                return
            if not self._check(query):
                return
            try:
                if url.path == "/":
                    page = EDITOR_HTML.replace("__TOKEN__", editor.token)
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8",
                               {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                                           "script-src 'self' 'unsafe-inline'; media-src 'self'"})
                elif url.path == "/api/state":
                    self._json(editor.state())
                elif url.path == "/api/source":
                    self._json(editor.source_media(int((query.get("i") or ["-1"])[0])))
                elif url.path.startswith("/media/"):
                    self._media(url.path[len("/media/"):])
                else:
                    self._error(HTTPStatus.NOT_FOUND, "not found")
            except EditError as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e))
            except (ValueError, OSError, RuntimeError) as e:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e)[-500:])

        def _media(self, key: str) -> None:
            path = editor.media.get(key)
            if path is None or not path.is_file():
                self._error(HTTPStatus.NOT_FOUND, "not found")
                return
            size = path.stat().st_size
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            start, end, status = 0, size - 1, 200
            m = _RANGE_RE.match(self.headers.get("Range") or "")
            if m and size:
                a, b = m.groups()
                if a:
                    start, end = int(a), min(size - 1, int(b) if b else size - 1)
                elif b:
                    start, end = max(0, size - int(b)), size - 1
                if start > end or start >= size:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
            length = end - start + 1 if size else 0
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):  # 再生位置を動かすとブラウザが切る
                        return
                    left -= len(chunk)

        # --- POST ---
        def do_POST(self):
            url = urlparse(self.path)
            if not self._check(parse_qs(url.query)):
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 5_000_000:
                    raise EditError("送る内容が大きすぎます")
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise EditError("送る内容の形式が違います")
                if url.path == "/api/save":
                    self._json({"edit": editor.save(body.get("i"), body.get("edit"))})
                elif url.path == "/api/render":
                    editor.render(body.get("i"), body.get("edit"))
                    self._json({"ok": True})
                elif url.path == "/api/silence":
                    e = clean_edit(body.get("edit"), editor.project.get("duration"))
                    editor.short(body.get("i"))
                    self._json({"cuts": editor.detect_silence(body.get("i"), e["start"], e["end"])})
                elif url.path == "/api/lang":
                    self._json({"lang": editor.set_language(body.get("lang"))})
                else:
                    self._error(HTTPStatus.NOT_FOUND, "not found")
            except (EditError, json.JSONDecodeError) as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e))
            except (ValueError, OSError, RuntimeError) as e:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(e)[-500:])

    return Handler


def serve(cfg: Config, run_dir: str | Path, port: int = 0, open_browser: bool = True) -> int:
    editor = Editor(cfg, run_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(editor))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}/?t={editor.token}"
    print(f"編集画面: {url}")
    print("閉じるときは Ctrl+C (タスクトレイから開いた場合は、ブラウザを閉じても裏で動き続けます)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


EDITOR_HTML = Path(__file__).with_name("editor.html").read_text(encoding="utf-8")
