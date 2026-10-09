"""チャットの取得・読み込み。

- VOD のチャットリプレイ: Twitch 公式 API には VOD チャット取得 API が無いため、Twitch の
  Web プレイヤー自身が使う GQL (``VideoCommentsByOffsetOrCursor``) を使う。非公式なので
  仕様変更で動かなくなる可能性がある。その場合は TwitchDownloader 等で保存した JSON を
  ``--chat`` で渡せば同じように動く。
- ライブ配信: Twitch の IRC (irc.chat.twitch.tv) に接続して JSONL に記録する。
"""

from __future__ import annotations

import json
import random
import re
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator

import requests

from .fileutil import read_jsonl
from .models import ChatMessage

GQL_URL = "https://gql.twitch.tv/gql"
# Twitch Web プレイヤーが使っている公開 Client-ID (TwitchDownloader 等の OSS と同じ値)
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_COMMENTS_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


# ---------------------------------------------------------------------------
# VOD chat replay (GQL)
# ---------------------------------------------------------------------------

def _parse_gql_comments(payload: dict) -> tuple[list[tuple[str, ChatMessage]], bool]:
    if isinstance(payload, list):
        payload = payload[0]
    if payload.get("errors"):
        raise RuntimeError(f"GQL エラー: {payload['errors']}")
    video = (payload.get("data") or {}).get("video")
    if video is None:
        raise RuntimeError("VOD が見つからないか、チャットが取得できません")
    comments = video.get("comments") or {}
    out: list[tuple[str, ChatMessage]] = []
    for edge in comments.get("edges") or []:
        node = edge.get("node") or {}
        frags = (node.get("message") or {}).get("fragments") or []
        text = "".join(f.get("text", "") for f in frags)
        emotes = [f["text"] for f in frags if f.get("emote")]
        commenter = node.get("commenter") or {}
        out.append(
            (
                node.get("id", ""),
                ChatMessage(
                    offset=float(node.get("contentOffsetSeconds", 0)),
                    user=commenter.get("login") or commenter.get("displayName") or "",
                    text=text,
                    emotes=emotes,
                ),
            )
        )
    has_next = bool((comments.get("pageInfo") or {}).get("hasNextPage"))
    return out, has_next


def fetch_vod_chat(
    video_id: str,
    session: requests.Session | None = None,
    progress: Callable[[float], None] | None = None,
    max_requests: int = 100_000,
) -> list[ChatMessage]:
    """VOD のチャットリプレイを全件取得する。

    カーソルではなく ``contentOffsetSeconds`` でページングする(カーソル指定は
    クライアント整合性チェックを要求されやすいため)。重複は ID で除外する。
    """
    session = session or requests.Session()
    headers = {"Client-Id": GQL_CLIENT_ID, "Content-Type": "text/plain;charset=UTF-8"}
    seen: set[str] = set()
    messages: list[ChatMessage] = []
    offset = 0.0
    for _ in range(max_requests):
        body = [
            {
                "operationName": "VideoCommentsByOffsetOrCursor",
                "variables": {"videoID": str(video_id), "contentOffsetSeconds": int(offset)},
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": GQL_COMMENTS_HASH}},
            }
        ]
        for attempt in range(5):
            r = session.post(GQL_URL, data=json.dumps(body), headers=headers, timeout=30)
            if r.status_code == 200:
                break
            time.sleep(2**attempt)
        else:
            raise RuntimeError(f"チャット取得に失敗しました: HTTP {r.status_code}")
        page, has_next = _parse_gql_comments(r.json())
        new = [(cid, m) for cid, m in page if cid not in seen]
        for cid, m in new:
            seen.add(cid)
            messages.append(m)
        if progress and messages:
            progress(messages[-1].offset)
        if not has_next or not page:
            break
        last = max(m.offset for _, m in page)
        # 同一秒にページ容量以上のコメントがあると進まないので 1 秒進める
        offset = last if (new and int(last) > int(offset)) else int(offset) + 1
    messages.sort(key=lambda m: m.offset)
    return messages


# ---------------------------------------------------------------------------
# Loaders / savers
# ---------------------------------------------------------------------------

def save_chat_jsonl(messages: Iterable[ChatMessage], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m.to_dict(), ensure_ascii=False) + "\n")


def load_chat(path: str | Path) -> list[ChatMessage]:
    """チャットファイルを読み込む。対応形式:

    - 本ツールの JSONL (``{"offset", "user", "text"}`` を1行ずつ)
    - TwitchDownloader の JSON (``comments[].content_offset_seconds`` / ``message.body``)
    - chat-downloader の JSON 配列 (``time_in_seconds`` / ``message``)
    - Twitch GQL レスポンスをそのまま保存したもの
    """
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 1 行 1 JSON の JSONL (記録中の停電などで壊れた行は飛ばす)
        msgs = []
        for d in read_jsonl(raw):
            try:
                msgs.append(ChatMessage.from_dict(d))
            except (KeyError, TypeError, ValueError):  # offset が無い・数値でない行は飛ばす
                continue
        return sorted(msgs, key=lambda m: m.offset)
    if isinstance(data, dict) and "offset" in data:  # 1 件だけの JSONL
        return [ChatMessage.from_dict(data)]
    msgs: list[ChatMessage] = []
    if isinstance(data, dict) and "comments" in data and isinstance(data["comments"], list):
        for c in data["comments"]:
            msg = c.get("message") or {}
            commenter = c.get("commenter") or {}
            frags = msg.get("fragments") or []
            emotes = [f.get("text", "") for f in frags if f.get("emoticon") or f.get("emote")]
            msgs.append(
                ChatMessage(
                    offset=float(c.get("content_offset_seconds", 0)),
                    user=commenter.get("name") or commenter.get("display_name") or "",
                    text=msg.get("body") or "".join(f.get("text", "") for f in frags),
                    emotes=emotes,
                )
            )
    elif isinstance(data, list) and data and "time_in_seconds" in data[0]:
        for c in data:
            author = c.get("author") or {}
            msgs.append(
                ChatMessage(
                    offset=float(c["time_in_seconds"]),
                    user=author.get("name", ""),
                    text=c.get("message", ""),
                    emotes=[e.get("name", "") for e in c.get("emotes") or []],
                )
            )
    elif isinstance(data, list) and data and "offset" in data[0]:
        msgs = [ChatMessage.from_dict(d) for d in data]
    elif isinstance(data, (dict, list)) and _looks_like_gql(data):
        msgs = [m for _, m in _parse_gql_comments(data)[0]]
    else:
        raise ValueError(f"チャットファイルの形式を判別できません: {path}")
    return sorted(msgs, key=lambda m: m.offset)


def _looks_like_gql(data) -> bool:
    d = data[0] if isinstance(data, list) and data else data
    return isinstance(d, dict) and isinstance(d.get("data"), dict) and "video" in d["data"]


# ---------------------------------------------------------------------------
# Live chat (IRC)
# ---------------------------------------------------------------------------

_IRC_RE = re.compile(
    r"^(?:@(?P<tags>\S+) )?:(?P<user>[^!\s]+)(?:!\S+)? PRIVMSG #(?P<channel>\S+) :(?P<text>.*)$"
)


def parse_irc_privmsg(line: str) -> tuple[str, str, list[str]] | None:
    """PRIVMSG 行を (user, text, emotes) に変換する。それ以外の行は None。"""
    m = _IRC_RE.match(line.rstrip("\r\n"))
    if not m:
        return None
    text = m.group("text")
    tags = {}
    if m.group("tags"):
        for kv in m.group("tags").split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    emotes: list[str] = []
    # emotes=25:0-4,12-16/1902:6-10 形式から絵文字名を取り出す
    for spec in filter(None, tags.get("emotes", "").split("/")):
        _, _, ranges = spec.partition(":")
        first = ranges.split(",")[0]
        try:
            a, b = (int(x) for x in first.split("-"))
            emotes.append(text[a : b + 1])
        except ValueError:
            continue
    user = tags.get("display-name") or m.group("user")
    return user, text, emotes


class LiveChatRecorder:
    """Twitch IRC に接続し、受信したチャットを JSONL に追記していくレコーダー。

    ``offset`` は ``start_time`` (録画開始時刻, time.time()) からの経過秒。
    認証情報が無い場合は匿名ユーザー (justinfanNNNN) で読み取り専用接続する。
    """

    HOST = "irc.chat.twitch.tv"
    PORT = 6697

    def __init__(self, channel: str, out_path: str | Path, start_time: float,
                 oauth_token: str = "", nick: str = ""):
        self.channel = channel.lower().lstrip("#")
        self.out_path = Path(out_path)
        self.start_time = start_time
        self.oauth_token = oauth_token
        self.nick = nick
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

    def _connect(self) -> ssl.SSLSocket:
        raw = socket.create_connection((self.HOST, self.PORT), timeout=30)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.HOST)
        if self.oauth_token:
            token = self.oauth_token if self.oauth_token.startswith("oauth:") else f"oauth:{self.oauth_token}"
            sock.sendall(f"PASS {token}\r\n".encode())
            sock.sendall(f"NICK {self.nick or 'justinfan'}\r\n".encode())
        else:
            sock.sendall(f"NICK justinfan{random.randint(10000, 99999)}\r\n".encode())
        sock.sendall(b"CAP REQ :twitch.tv/tags\r\n")
        sock.sendall(f"JOIN #{self.channel}\r\n".encode())
        sock.settimeout(1.0)
        return sock

    def _lines(self, sock) -> Iterator[str]:
        buf = b""
        last_data = time.time()
        while not self._stop.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                if time.time() - last_data > 360:  # PING も来ないなら切断扱い
                    raise ConnectionError("IRC タイムアウト")
                continue
            if not chunk:
                raise ConnectionError("IRC 切断")
            last_data = time.time()
            buf += chunk
            *lines, buf = buf.split(b"\r\n")
            for line in lines:
                yield line.decode("utf-8", errors="replace")

    def _run(self) -> None:
        backoff = 1
        with open(self.out_path, "a", encoding="utf-8") as f:
            while not self._stop.is_set():
                try:
                    sock = self._connect()
                    backoff = 1
                    for line in self._lines(sock):
                        if line.startswith("PING"):
                            sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                            continue
                        parsed = parse_irc_privmsg(line)
                        if not parsed:
                            continue
                        user, text, emotes = parsed
                        msg = ChatMessage(time.time() - self.start_time, user, text, emotes)
                        f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
                        f.flush()
                        self.count += 1
                    sock.close()
                except (OSError, ConnectionError):
                    if self._stop.wait(backoff):
                        break
                    backoff = min(backoff * 2, 60)
