import json
import time
from datetime import datetime, timezone

import pytest

from twitch_shorts import cli, watcher
from twitch_shorts.config import Config
from twitch_shorts.models import Highlight
from twitch_shorts.twitch_api import VideoInfo
from twitch_shorts.twitch_auth import TwitchAuthError, UserToken, device_login, load_token, save_token
from twitch_shorts.twitch_clips import ClipCreator, ClipError, clip_window, create_clips

from .conftest import FakeResponse, FakeSession


class Resp(FakeResponse):
    def __init__(self, payload, status=200, headers=None):
        super().__init__(payload, status, {"content-type": "application/json", **(headers or {})})


def test_device_login_waits_for_approval_and_saves_token(tmp_path):
    polls = []

    def handler(method, url, params, data):
        if url.endswith("/device"):
            assert "channel:manage:clips" in data["scopes"]
            return Resp({"device_code": "dc", "user_code": "ABCD", "verification_uri": "https://www.twitch.tv/activate",
                         "expires_in": 1800, "interval": 5})
        if url.endswith("/token"):
            polls.append(data)
            assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
            if len(polls) < 3:
                return Resp({"status": 400, "message": "authorization_pending"}, status=400)
            return Resp({"access_token": "at", "refresh_token": "rt", "expires_in": 14000})
        if url.endswith("/validate"):
            return Resp({"user_id": "42", "login": "yuuki_ftw", "scopes": ["channel:manage:clips", "clips:edit"]})
        raise AssertionError(url)

    shown = []
    token = device_login("cid", "secret", tmp_path, show=lambda u, c: shown.append((u, c)),
                         session=FakeSession(handler), sleep=lambda s: None)
    assert shown == [("https://www.twitch.tv/activate", "ABCD")]
    assert len(polls) == 3 and polls[0]["client_secret"] == "secret"
    assert token["user_id"] == "42" and load_token(tmp_path)["refresh_token"] == "rt"


def test_device_login_denied(tmp_path):
    def handler(method, url, params, data):
        if url.endswith("/device"):
            return Resp({"device_code": "dc", "verification_uri": "u", "expires_in": 60, "interval": 1})
        return Resp({"status": 400, "message": "access_denied"}, status=400)

    with pytest.raises(TwitchAuthError):
        device_login("cid", "", tmp_path, show=lambda u, c: None, session=FakeSession(handler), sleep=lambda s: None)


def _token(tmp_path, expires_at=None):
    save_token(tmp_path, {"access_token": "old", "refresh_token": "rt", "expires_at": expires_at or time.time() + 9999,
                          "user_id": "42", "login": "yuuki_ftw", "scopes": ["channel:manage:clips"]})


def test_user_token_refreshes_when_expired(tmp_path):
    _token(tmp_path, expires_at=time.time() - 1)

    def handler(method, url, params, data):
        assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "rt"
        return Resp({"access_token": "new", "refresh_token": "rt2", "expires_in": 14000})

    t = UserToken("cid", "secret", tmp_path, session=FakeSession(handler))
    assert t.access_token() == "new"
    assert load_token(tmp_path)["refresh_token"] == "rt2"


def test_user_token_requires_login(tmp_path):
    with pytest.raises(TwitchAuthError):
        UserToken("cid", "", tmp_path)


def test_clip_from_vod_params_and_title_fallback(tmp_path):
    _token(tmp_path)
    calls = []

    def handler(method, url, params, data):
        calls.append(dict(params))
        assert url.endswith("/helix/videos/clips")
        if "title" in params:
            return Resp({"message": "unknown parameter"}, status=400)
        return Resp({"data": [{"id": "Clip1", "edit_url": "https://clips.twitch.tv/Clip1/edit"}]})

    creator = ClipCreator("cid", UserToken("cid", "", tmp_path), session=FakeSession(handler))
    clip = creator.from_vod("v9", 125.4, 80, "神プレイ")
    assert clip["id"] == "Clip1"
    first, second = calls
    assert first["vod_offset"] == 125 and first["duration"] == 60 and first["title"] == "神プレイ"
    assert first["broadcaster_id"] == first["editor_id"] == "42"
    assert "title" not in second


def test_clip_forbidden_stops(tmp_path):
    _token(tmp_path)
    session = FakeSession(lambda *a: Resp({"message": "forbidden"}, status=403))
    creator = ClipCreator("cid", UserToken("cid", "", tmp_path), session=session)
    hs = [Highlight(start=0, end=30, peak=20, score=s, title=str(s)) for s in (1, 2)]
    assert create_clips(creator, "v9", hs) == []
    assert len(session.calls) == 1  # 権限が無いと分かったら残りは試さない
    with pytest.raises(ClipError):
        creator.from_vod("v9", 30, 30)


def test_clip_window_fits_twitch_limits():
    assert clip_window(Highlight(start=100, end=130, peak=120, score=1)) == (130, 30)
    assert clip_window(Highlight(start=100, end=130, peak=120, score=1), shift=12.5) == (142.5, 30)
    end, dur = clip_window(Highlight(start=0, end=90, peak=70, score=1))
    assert dur == 60 and end - dur <= 70 <= end  # 60 秒に縮めてもピークを含む
    assert clip_window(Highlight(start=0, end=2, peak=1, score=1)) == (5, 5)


def test_find_stream_vod_uses_recording_offset():
    started = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

    class FakeHelix:
        def get_user_id(self, login):
            return "42"

        def get_recent_archives(self, uid, count):
            return [VideoInfo("cur", "42", "yuuki_ftw", "Y", "t", started, 600, "u")]

    rec_start = started.timestamp() + 95
    assert watcher.find_stream_vod(FakeHelix(), "yuuki_ftw", rec_start) == ("cur", 95)
    # 録画開始より後に始まった VOD (別の配信) は使わない
    assert watcher.find_stream_vod(FakeHelix(), "yuuki_ftw", started.timestamp() - 3600) is None


def test_pipeline_creates_clips_when_enabled(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import twitch_clips
    from twitch_shorts.chat import load_chat
    from twitch_shorts.pipeline import LocalSource, process

    video, chat_path = make_stream(duration=100, events=(50,))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    cfg.clips.enabled = True
    _token(cfg.work_dir)
    made = []

    def fake_from_vod(self, vod_id, end, duration, title=""):
        made.append((vod_id, end, duration))
        return {"id": "C", "edit_url": "https://clips.twitch.tv/C/edit"}

    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", fake_from_vod)
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), tmp_path / "out" / "run",
                clip_vod=("v1", 30.0))
    [h] = r.highlights
    [(vod_id, end, dur)] = made
    assert vod_id == "v1" and end == pytest.approx(h.end + 30) and dur == pytest.approx(h.end - h.start)
    report = json.loads((tmp_path / "out" / "run" / "highlights.json").read_text(encoding="utf-8"))
    assert report["highlights"][0]["twitch_clip"] == "https://clips.twitch.tv/C/edit"


def test_auto_enables_clips_only_for_logged_in_broadcaster(tmp_path, monkeypatch):
    import twitch_shorts.watcher as w

    seen = {}
    monkeypatch.setattr(w, "watch", lambda cfg, channel, once=False: seen.update(channel=channel,
                                                                                clips=cfg.clips.enabled))
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n[twitch]\nclient_id = "cid"\nclient_secret = "s"\n')
    assert cli.main(["-c", str(conf), "auto", "Yuuki_FTW"]) == 0
    assert seen == {"channel": "yuuki_ftw", "clips": False}  # 未ログイン
    _token(tmp_path / "work")
    assert cli.main(["-c", str(conf), "auto", "yuuki_ftw"]) == 0
    assert seen["clips"] is True
    assert cli.main(["-c", str(conf), "auto", "someone_else"]) == 0
    assert seen["clips"] is False  # 他人のチャンネルでは作らない
    assert cli.main(["-c", str(conf), "auto", "yuuki_ftw", "--no-twitch-clips"]) == 0
    assert seen["clips"] is False


def test_doctor_command(tmp_path, capsys):
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n')
    assert cli.main(["-c", str(conf), "doctor"]) == 0
    out = capsys.readouterr().out
    assert "ffmpeg" in out and "Twitch ログイン" in out
