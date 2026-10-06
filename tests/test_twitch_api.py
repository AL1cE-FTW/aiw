from datetime import datetime, timezone

import pytest

from twitch_shorts.twitch_api import (
    HelixClient, TwitchAPIError, VideoInfo, parse_twitch_duration, parse_video_id,
)

from .conftest import FakeResponse, FakeSession


def test_parse_twitch_duration():
    assert parse_twitch_duration("3h8m33s") == 3 * 3600 + 8 * 60 + 33
    assert parse_twitch_duration("45s") == 45
    assert parse_twitch_duration("2h") == 7200
    with pytest.raises(ValueError):
        parse_twitch_duration("abc")


@pytest.mark.parametrize("value", [
    "https://www.twitch.tv/videos/2245678901",
    "https://www.twitch.tv/yuuki_ftw/video/2245678901",
    "2245678901", "v2245678901",
])
def test_parse_video_id(value):
    assert parse_video_id(value) == "2245678901"


def test_missing_credentials_raise():
    with pytest.raises(TwitchAPIError):
        HelixClient("", "")


def _video():
    return VideoInfo(id="111111", user_id="42", user_login="yuuki_ftw", user_name="Yuuki", title="t",
                     created_at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc), duration=7200, url="u")


def test_get_clips_for_video_filters_and_paginates():
    def handler(method, url, params, data):
        if method == "POST":
            return FakeResponse({"access_token": "tok", "expires_in": 3600})
        if "after" not in params:
            return FakeResponse({"data": [
                {"video_id": "111111", "vod_offset": 100, "duration": 30, "view_count": 50, "title": "a"},
                {"video_id": "999999", "vod_offset": 10, "duration": 30, "view_count": 5},
                {"video_id": "111111", "vod_offset": None, "duration": 30, "view_count": 5},
            ], "pagination": {"cursor": "next"}})
        return FakeResponse({"data": [
            {"video_id": "111111", "vod_offset": 900, "duration": 20.5, "view_count": 3, "title": "b"},
        ], "pagination": {}})

    session = FakeSession(handler)
    clips = HelixClient("id", "secret", session=session).get_clips_for_video(_video())
    assert [(c.offset, c.views) for c in clips] == [(100, 50), (900, 3)]
    gets = [c for c in session.calls if c[0] == "GET"]
    assert gets[0][2]["broadcaster_id"] == "42"
    assert gets[0][2]["started_at"] == "2026-10-01T12:00:00Z"
    assert gets[0][3]["Authorization"] == "Bearer tok"
    assert sum(1 for c in session.calls if c[0] == "POST") == 1  # トークンは使い回す


def test_retries_once_on_expired_token():
    state = {"tokens": 0, "gets": 0}

    def handler(method, url, params, data):
        if method == "POST":
            state["tokens"] += 1
            return FakeResponse({"access_token": f"t{state['tokens']}", "expires_in": 3600})
        state["gets"] += 1
        if state["gets"] == 1:
            return FakeResponse({"message": "invalid token"}, status=401)
        return FakeResponse({"data": [{"id": "42"}]})

    assert HelixClient("id", "s", session=FakeSession(handler)).get_user_id("Yuuki_FTW") == "42"
    assert state["tokens"] == 2


def test_get_recent_archives():
    def handler(method, url, params, data):
        if method == "POST":
            return FakeResponse({"access_token": "tok", "expires_in": 3600})
        assert params["type"] == "archive"
        return FakeResponse({"data": [{
            "id": "5", "user_id": "42", "user_login": "yuuki_ftw", "user_name": "Yuuki", "title": "配信",
            "created_at": "2026-10-01T12:00:00Z", "duration": "1h2m3s", "url": "https://www.twitch.tv/videos/5",
        }]})

    [v] = HelixClient("id", "s", session=FakeSession(handler)).get_recent_archives("42")
    assert v.duration == 3723 and v.user_login == "yuuki_ftw"
