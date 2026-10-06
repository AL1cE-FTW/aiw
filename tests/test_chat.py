import json

from twitch_shorts.chat import fetch_vod_chat, load_chat, parse_irc_privmsg, save_chat_jsonl
from twitch_shorts.models import ChatMessage

from .conftest import FakeResponse, FakeSession


def _gql_page(comments, has_next):
    return [{
        "data": {"video": {"comments": {
            "edges": [{"cursor": f"c{cid}", "node": {
                "id": cid, "contentOffsetSeconds": off,
                "commenter": {"login": user, "displayName": user.upper()},
                "message": {"fragments": [{"text": text, "emote": None}]},
            }} for cid, off, user, text in comments],
            "pageInfo": {"hasNextPage": has_next},
        }}}
    }]


def test_fetch_vod_chat_pages_by_offset_and_dedupes():
    pages = {
        0: _gql_page([("1", 0, "a", "hi"), ("2", 5, "b", "草")], True),
        5: _gql_page([("2", 5, "b", "草"), ("3", 9, "c", "KEKW")], True),
        9: _gql_page([("3", 9, "c", "KEKW")], False),
    }

    def handler(method, url, params, data):
        body = json.loads(data)[0]
        assert body["operationName"] == "VideoCommentsByOffsetOrCursor"
        return FakeResponse(pages[body["variables"]["contentOffsetSeconds"]])

    session = FakeSession(handler)
    msgs = fetch_vod_chat("123456789", session=session)
    assert [m.text for m in msgs] == ["hi", "草", "KEKW"]
    assert [m.user for m in msgs] == ["a", "b", "c"]
    assert all(c[3]["Client-Id"] for c in session.calls)


def test_fetch_vod_chat_advances_when_stuck_on_same_second():
    calls = []

    def handler(method, url, params, data):
        off = json.loads(data)[0]["variables"]["contentOffsetSeconds"]
        calls.append(off)
        if off == 0:
            return FakeResponse(_gql_page([("1", 0, "a", "x")], True))
        return FakeResponse(_gql_page([("2", 1, "b", "y")], False))

    msgs = fetch_vod_chat("123456789", session=FakeSession(handler))
    assert calls == [0, 1]
    assert len(msgs) == 2


def test_load_jsonl_roundtrip(tmp_path):
    p = tmp_path / "c.jsonl"
    save_chat_jsonl([ChatMessage(2, "b", "後"), ChatMessage(1, "a", "前", ["Kappa"])], p)
    msgs = load_chat(p)
    assert [m.text for m in msgs] == ["前", "後"]
    assert msgs[0].emotes == ["Kappa"]


def test_load_twitchdownloader_json(tmp_path):
    p = tmp_path / "td.json"
    p.write_text(json.dumps({"comments": [
        {"content_offset_seconds": 12.5, "commenter": {"name": "bob"},
         "message": {"body": "LUL 草", "fragments": [{"text": "LUL", "emoticon": {"emoticon_id": "1"}},
                                                    {"text": " 草", "emoticon": None}]}},
    ]}), encoding="utf-8")
    [m] = load_chat(p)
    assert (m.offset, m.user, m.text, m.emotes) == (12.5, "bob", "LUL 草", ["LUL"])


def test_load_chat_downloader_json(tmp_path):
    p = tmp_path / "cd.json"
    p.write_text(json.dumps([{"time_in_seconds": 3, "message": "pog", "author": {"name": "z"}}]))
    [m] = load_chat(p)
    assert (m.offset, m.user, m.text) == (3, "z", "pog")


def test_load_raw_gql_response(tmp_path):
    p = tmp_path / "gql.json"
    p.write_text(json.dumps(_gql_page([("1", 4, "a", "hello")], False)))
    [m] = load_chat(p)
    assert (m.offset, m.text) == (4, "hello")


def test_parse_irc_privmsg_with_tags_and_emotes():
    line = ("@badge-info=;display-name=Yuuki;emotes=25:0-4;id=abc :yuuki!yuuki@yuuki.tmi.twitch.tv "
            "PRIVMSG #yuuki_ftw :Kappa 草\r\n")
    assert parse_irc_privmsg(line) == ("Yuuki", "Kappa 草", ["Kappa"])


def test_parse_irc_ignores_non_privmsg():
    assert parse_irc_privmsg("PING :tmi.twitch.tv") is None
    assert parse_irc_privmsg(":tmi.twitch.tv 001 justinfan123 :Welcome, GLHF!") is None
