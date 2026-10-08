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
            return Resp({"message": "invalid parameter: title"}, status=400)
        return Resp({"data": [{"id": "Clip1", "edit_url": "https://clips.twitch.tv/Clip1/edit"}]})

    creator = ClipCreator("cid", UserToken("cid", "", tmp_path), session=FakeSession(handler))
    clip = creator.from_vod("v9", 125.4, 80, "神プレイ")
    assert clip["id"] == "Clip1"
    first, second = calls
    assert first["vod_offset"] == 126 and first["duration"] == 60 and first["title"] == "神プレイ"
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
    assert clip_window(Highlight(start=100, end=130, peak=120, score=1), shift=12.5) == (143, 30)  # 整数秒に切り上げ
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
    cfg.twitch.client_id = "cid"
    _token(cfg.work_dir)
    made = []

    def fake_from_vod(self, vod_id, end, duration, title=""):
        made.append((vod_id, end, duration))
        return {"id": "C", "edit_url": "https://clips.twitch.tv/C/edit"}

    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", fake_from_vod)
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), tmp_path / "out" / "run",
                clip_vod=("v1", 30.0), channel="yuuki_ftw")
    [h] = r.highlights
    [(vod_id, end, dur)] = made
    assert vod_id == "v1" and end == pytest.approx(h.end + 30) and dur == pytest.approx(h.end - h.start)
    report = json.loads((tmp_path / "out" / "run" / "highlights.json").read_text(encoding="utf-8"))
    # 共有用の公開 URL と、配信者用の編集ページを分けて記録する
    assert report["highlights"][0]["twitch_clip"] == "https://clips.twitch.tv/C"
    assert report["highlights"][0]["twitch_clip_edit"] == "https://clips.twitch.tv/C/edit"


def test_auto_enables_clips_only_for_logged_in_broadcaster(tmp_path, monkeypatch):
    import twitch_shorts.watcher as w

    seen = {}
    monkeypatch.setattr(w, "watch", lambda cfg, channel, once=False, **k: seen.update(channel=channel,
                                                                                     clips=cfg.clips.enabled,
                                                                                     dry_run=k.get("dry_run")))
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n[twitch]\nclient_id = "cid"\nclient_secret = "s"\n')
    assert cli.main(["-c", str(conf), "auto", "Yuuki_FTW"]) == 0
    assert seen == {"channel": "yuuki_ftw", "clips": False, "dry_run": False}  # 未ログイン
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



# --- レビュー指摘の回帰テスト -------------------------------------------------

def test_find_stream_vod_rejects_previous_stream():
    yesterday = datetime(2026, 10, 7, 11, 0, tzinfo=timezone.utc)

    class FakeHelix:
        def get_user_id(self, login):
            return "42"

        def get_recent_archives(self, uid, count):
            return [VideoInfo("old", "42", "yuuki_ftw", "Y", "t", yesterday, 3 * 3600, "u")]

    today = yesterday.timestamp() + 86400
    assert watcher.find_stream_vod(FakeHelix(), "yuuki_ftw", today) is None
    # 配信の開始時刻が分かれば、それと一致する VOD だけを使う
    assert watcher.find_stream_vod(FakeHelix(), "yuuki_ftw", today, stream_started=today - 60) is None
    assert watcher.find_stream_vod(FakeHelix(), "yuuki_ftw", yesterday.timestamp() + 30,
                                   stream_started=yesterday.timestamp()) == ("old", 30)


def _clip_setup(tmp_path, make_stream):
    from twitch_shorts.chat import load_chat

    video, chat_path = make_stream(duration=100, events=(50,))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    cfg.clips.enabled = True
    cfg.twitch.client_id = "cid"
    _token(cfg.work_dir)
    return cfg, video, load_chat(chat_path)


def test_clips_are_not_duplicated_on_rerun(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import twitch_clips
    from twitch_shorts.pipeline import LocalSource, process

    cfg, video, chat = _clip_setup(tmp_path, make_stream)
    calls = []
    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod",
                        lambda self, v, e, d, t="": calls.append(e) or {"id": "C", "edit_url": "https://clips.twitch.tv/C"})
    for _ in range(2):
        r = process(cfg, LocalSource(str(video)), chat, tmp_path / "out" / "run", clip_vod=("v1", 0.0),
                    channel="yuuki_ftw", dry_run=False)
        assert r.highlights[0].twitch_clip == "https://clips.twitch.tv/C"
    assert len(calls) == 1  # 2 回目は記録済みのクリップを使う


def test_clips_skipped_for_other_channels_and_errors_do_not_abort(make_stream, tmp_path, monkeypatch):
    import requests

    from twitch_shorts import twitch_clips
    from twitch_shorts.pipeline import LocalSource, process

    cfg, video, chat = _clip_setup(tmp_path, make_stream)
    calls = []
    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", lambda *a, **k: calls.append(a) or {})
    r = process(cfg, LocalSource(str(video)), chat, tmp_path / "o1", clip_vod=("v1", 0.0), channel="someone_else")
    assert calls == [] and r.highlights[0].twitch_clip == ""

    def boom(*a, **k):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", boom)
    r = process(cfg, LocalSource(str(video)), chat, tmp_path / "o2", clip_vod=("v1", 0.0), channel="yuuki_ftw")
    assert r.highlights and (tmp_path / "o2" / "highlights.json").exists() and (tmp_path / "o2" / "index.html").exists()


def test_templates_with_unknown_fields_do_not_crash():
    from twitch_shorts.pipeline import _fill_post_text, fill_template

    assert fill_template("{channel} #{index} {unknown}", {"channel": "y", "index": 2}) == "y #2 {unknown}"
    assert fill_template("壊れた {", {"channel": "y"}) == "壊れた {"
    cfg = Config()
    cfg.publish.description_template = "{channel} 切り抜き #{index} ({date})"
    h = Highlight(start=0, end=30, peak=10, score=1)
    _fill_post_text(cfg, h, "yuuki_ftw", 3)
    assert h.description.startswith("yuuki_ftw 切り抜き #3 (")


def test_corrupt_token_is_treated_as_logged_out(tmp_path):
    from twitch_shorts.twitch_auth import token_path

    token_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    token_path(tmp_path).write_text('{"access_token": "x", "refr', encoding="utf-8")
    assert load_token(tmp_path) is None
    with pytest.raises(TwitchAuthError):
        UserToken("cid", "", tmp_path)
    _token(tmp_path)  # 保存し直すと読める
    assert load_token(tmp_path)["login"] == "yuuki_ftw"


def test_auto_requires_clip_scope(tmp_path, monkeypatch):
    import twitch_shorts.watcher as w

    seen = {}
    monkeypatch.setattr(w, "watch", lambda cfg, channel, once=False, **k: seen.update(clips=cfg.clips.enabled))
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n[twitch]\nclient_id = "cid"\nclient_secret = "s"\n')
    save_token(tmp_path / "work", {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 999,
                                   "user_id": "42", "login": "yuuki_ftw", "scopes": ["user:read:email"]})
    assert cli.main(["-c", str(conf), "auto", "yuuki_ftw"]) == 0
    assert seen["clips"] is False


def test_doctor_unknown_font_and_forced_recorder(monkeypatch):
    from twitch_shorts import doctor

    real_which = doctor.shutil.which
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None if n in ("fc-list", "streamlink") else real_which(n))
    cfg = Config()
    cfg.watch.recorder = "streamlink"
    checks = {c.name: c for c in doctor.run_checks(cfg)}
    assert checks["日本語フォント"].ok is None
    assert checks["録画ツール (streamlink)"].ok is False  # yt-dlp があっても streamlink 指定なら NG



# --- 2 回目のレビュー指摘の回帰テスト ---------------------------------------

def test_auto_passes_dry_run(tmp_path, monkeypatch):
    import twitch_shorts.watcher as w

    seen = {}
    monkeypatch.setattr(w, "watch", lambda cfg, channel, once=False, dry_run=False: seen.update(dry_run=dry_run))
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n')
    assert cli.main(["-c", str(conf), "auto", "yuuki_ftw", "--dry-run"]) == 0
    assert seen["dry_run"] is True


def test_dry_run_watch_session_creates_no_clips(make_stream, tmp_path, monkeypatch):
    import shutil
    import subprocess

    from twitch_shorts import twitch_clips

    video, chat_path = make_stream(duration=100, events=(50,))
    monkeypatch.setattr(watcher, "start_live_recording", lambda c, out, r, q: subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c", "copy", "-f", "mpegts", str(out)]))

    class FakeChat:
        def __init__(self, channel, out_path, *a):
            self.out_path, self.count = out_path, 0

        def start(self):
            shutil.copy(chat_path, self.out_path)

        def stop(self):
            pass

    class FakeHelix:
        def get_stream(self, login):
            return {"viewer_count": 3, "started_at": "2026-10-08T12:00:00Z"}

        def get_user_id(self, login):
            return "42"

        def get_recent_archives(self, uid, n):
            return [VideoInfo("v1", "42", "yuuki_ftw", "Y", "t", datetime(2026, 10, 8, 12, tzinfo=timezone.utc), 999, "u")]

    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", lambda *a, **k: pytest.fail("clip created in dry run"))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.clips.enabled = True
    _token(cfg.work_dir)
    done = watcher.record_and_process(cfg, "yuuki_ftw", helix=FakeHelix(), dry_run=True)
    assert done and all(not h.output_path and not h.twitch_clip for h in done)
    assert all(h.vod_url.startswith("https://www.twitch.tv/videos/v1?t=") for h in done)


def test_find_stream_vod_back_to_back_streams():
    prev_start = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)

    class FakeHelix:
        def get_user_id(self, login):
            raise AssertionError("user_id は渡したものを使う")

        def get_recent_archives(self, uid, count):
            return [VideoInfo("prev", "42", "yuuki_ftw", "Y", "t", prev_start, 3600, "u")]

    # 前の配信が終わった 5 分後に録画開始 (開始時刻は不明) → 前の VOD は選ばない
    assert watcher.find_stream_vod(FakeHelix(), "yuuki_ftw", prev_start.timestamp() + 3600 + 300,
                                   user_id="42") is None


def test_earlier_highlights_get_clips_once_vod_is_found(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import twitch_clips
    from twitch_shorts.pipeline import LocalSource, process

    cfg, video, chat = _clip_setup(tmp_path, make_stream)
    made = []
    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod",
                        lambda self, v, e, d, t="": made.append(e) or {"id": "C", "edit_url": f"https://clips.twitch.tv/{e}"})
    earlier = Highlight(start=5, end=30, peak=20, score=9, title="前半", output_path="/x/early.mp4")
    process(cfg, LocalSource(str(video)), chat, tmp_path / "o", clip_vod=("v1", 0.0), channel="yuuki_ftw",
            exclude=[earlier])
    assert earlier.twitch_clip and earlier.vod_url.endswith("t=0h0m5s")
    assert len(made) == 2


def test_clip_offset_never_below_duration_and_other_400_is_not_retried(tmp_path):
    _token(tmp_path)
    calls = []

    def handler(method, url, params, data):
        calls.append(dict(params))
        return Resp({"message": "vod_offset out of range"}, status=400)

    creator = ClipCreator("cid", UserToken("cid", "", tmp_path), session=FakeSession(handler))
    with pytest.raises(ClipError):
        creator.from_vod("v9", 5.3, 5.3, "タイトル")
    assert len(calls) == 1  # title 以外のエラーで title を外した再試行はしない
    assert calls[0]["vod_offset"] >= calls[0]["duration"]


def test_template_type_errors_and_list_config(tmp_path):
    from twitch_shorts.config import load_config
    from twitch_shorts.pipeline import fill_template

    assert fill_template("{index[0]} {channel}", {"index": 1, "channel": "y"}) == "{index[0]} {channel}"
    p = tmp_path / "c.toml"
    p.write_text('[publish]\nhashtags = "#shorts #Twitch切り抜き"\n', encoding="utf-8")
    assert load_config(p).publish.hashtags == ["#shorts", "#Twitch切り抜き"]
    p.write_text('[render]\nfacecam = "0,0,1,1"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(p)


def test_token_file_is_private(tmp_path):
    import os
    import stat

    _token(tmp_path)
    from twitch_shorts.twitch_auth import token_path

    if os.name == "posix":
        assert stat.S_IMODE(token_path(tmp_path).stat().st_mode) == 0o600



# --- 3 回目のレビュー指摘の回帰テスト ---------------------------------------

def test_final_pass_retries_vod_lookup_and_applies_latency(make_stream, tmp_path, monkeypatch):
    import shutil
    import subprocess

    video, chat_path = make_stream(duration=100, events=(50,))
    monkeypatch.setattr(watcher, "start_live_recording", lambda c, out, r, q: subprocess.Popen(
        ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c", "copy", "-f", "mpegts", str(out)]))

    class FakeChat:
        def __init__(self, channel, out_path, *a):
            self.out_path, self.count = out_path, 0

        def start(self):
            shutil.copy(chat_path, self.out_path)

        def stop(self):
            pass

    started = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    lookups = []

    class FakeHelix:
        def get_stream(self, login):
            return None  # 開始時刻は不明

        def get_user_id(self, login):
            return "42"

        def get_recent_archives(self, uid, n):
            lookups.append(1)
            return [VideoInfo("v1", "42", "yuuki_ftw", "Y", "t", started, 99999, "u")]

    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    rec_start = started.timestamp() + 300
    monkeypatch.setattr(watcher.time, "time", lambda: rec_start)
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.watch.rolling_minutes = 0
    cfg.watch.stream_latency = 8.0
    done = watcher.record_and_process(cfg, "yuuki_ftw", helix=FakeHelix(), dry_run=True)
    assert lookups, "最後の処理では待ち時間に関係なく VOD を探す"
    h = done[0]
    # 録画の位置 + (録画開始 - 遅れ - 配信開始) = VOD の位置
    expected = int(h.start + 300 - 8)
    assert h.vod_url.endswith(f"t={expected // 3600}h{expected % 3600 // 60}m{expected % 60}s")


def test_clip_limit_counts_earlier_runs_on_same_vod(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import twitch_clips
    from twitch_shorts.pipeline import LocalSource, process

    cfg, video, chat = _clip_setup(tmp_path, make_stream)
    cfg.clips.max_per_stream = 1
    (tmp_path / "work" / "twitch_clips.json").write_text(json.dumps([
        {"vod_id": "v1", "end": 9999, "duration": 30, "url": "https://clips.twitch.tv/old"}]), encoding="utf-8")
    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", lambda *a, **k: pytest.fail("上限を超えて作った"))
    r = process(cfg, LocalSource(str(video)), chat, tmp_path / "o", clip_vod=("v1", 0.0), channel="yuuki_ftw")
    assert r.highlights[0].twitch_clip == ""


def test_registry_with_wrong_shape_is_ignored(tmp_path):
    from twitch_shorts.twitch_clips import _load_registry, clips_made_for

    p = tmp_path / "twitch_clips.json"
    p.write_text("{}", encoding="utf-8")
    assert _load_registry(p) == [] and clips_made_for(p, "v1") == 0
    p.write_text('[{"vod_id": "v1", "end": 30, "url": "u"}, {"vod_id": "v1"}, "junk", 3]', encoding="utf-8")
    assert clips_made_for(p, "v1") == 1  # url や end の無い行は数えない


def test_permission_error_is_typed(tmp_path):
    from twitch_shorts.twitch_clips import ClipPermissionError

    _token(tmp_path)
    creator = ClipCreator("cid", UserToken("cid", "", tmp_path),
                          session=FakeSession(lambda *a: Resp({"message": "Forbidden"}, status=403)))
    with pytest.raises(ClipPermissionError):
        creator.from_vod("v9", 30, 30)


def test_clips_need_scope_for_every_command(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import twitch_clips
    from twitch_shorts.pipeline import LocalSource, process

    cfg, video, chat = _clip_setup(tmp_path, make_stream)
    save_token(cfg.work_dir, {"access_token": "a", "refresh_token": "r", "expires_at": time.time() + 999,
                              "user_id": "42", "login": "yuuki_ftw", "scopes": ["user:read:email"]})
    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", lambda *a, **k: pytest.fail("権限なしで作った"))
    process(cfg, LocalSource(str(video)), chat, tmp_path / "o", clip_vod=("v1", 0.0), channel="yuuki_ftw")


def test_schedule_picks_up_clip_created_later(tmp_path):
    from twitch_shorts.schedule import add_to_schedule, load_schedule

    cfg = Config(output_dir=str(tmp_path / "out"))
    h = Highlight(start=0, end=30, peak=5, score=1, title="a", output_path="/x/a.mp4")
    add_to_schedule(cfg, [h])
    h.twitch_clip = "https://clips.twitch.tv/late"
    add_to_schedule(cfg, [h])
    [e] = load_schedule(cfg)
    assert e["twitch_clip"] == "https://clips.twitch.tv/late"


def test_doctor_names_missing_ffprobe(monkeypatch):
    from twitch_shorts import doctor

    real = doctor.shutil.which
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None if n == "ffprobe" else real(n))
    [ff] = [c for c in doctor.run_checks(Config()) if c.name == "ffmpeg"]
    assert ff.ok is False and "ffprobe" in ff.detail



# --- 4 回目のレビュー指摘の回帰テスト ---------------------------------------

def test_ctrl_c_processes_then_exits(make_stream, tmp_path, monkeypatch):
    import shutil
    import subprocess

    video, chat_path = make_stream(duration=60, events=(30,))

    class Proc:
        returncode = None
        stderr = None

        def __init__(self, out):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c", "copy", "-f", "mpegts", str(out)],
                           check=True)

        def poll(self):
            return None

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    class FakeChat:
        def __init__(self, channel, out_path, *a):
            self.out_path, self.count = out_path, 0

        def start(self):
            shutil.copy(chat_path, self.out_path)

        def stop(self):
            pass

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher, "start_live_recording", lambda c, out, r, q: Proc(out))
    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher.time, "sleep", interrupt)
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    with pytest.raises(KeyboardInterrupt):
        watcher.record_and_process(cfg, "yuuki_ftw", dry_run=True)
    # 中断前に、ここまでの分は処理されている
    assert list((tmp_path / "out" / "yuuki_ftw").glob("*/highlights.json"))


def test_existing_clips_are_linked_even_when_limit_reached(tmp_path):
    from twitch_shorts.twitch_clips import create_clips

    reg = tmp_path / "twitch_clips.json"
    reg.write_text(json.dumps([{"vod_id": "v1", "end": 30, "url": "https://clips.twitch.tv/A"},
                               {"vod_id": "v1", "end": 130, "url": "https://clips.twitch.tv/B"}]), encoding="utf-8")
    hs = [Highlight(start=0, end=30, peak=10, score=1), Highlight(start=100, end=130, peak=110, score=9),
          Highlight(start=200, end=230, peak=210, score=5)]
    made = create_clips(None, "v1", hs, limit=0, registry=reg)
    assert made == []
    assert [h.twitch_clip for h in hs] == ["https://clips.twitch.tv/A", "https://clips.twitch.tv/B", ""]


def test_reused_clips_do_not_use_up_new_clip_slots(tmp_path):
    from twitch_shorts.twitch_clips import create_clips

    reg = tmp_path / "twitch_clips.json"
    reg.write_text(json.dumps([{"vod_id": "v1", "end": 130, "url": "https://clips.twitch.tv/B"}]), encoding="utf-8")

    class Creator:
        def from_vod(self, vod_id, end, duration, title=""):
            return {"id": f"N{int(end)}", "edit_url": "e"}

    hs = [Highlight(start=100, end=130, peak=110, score=9), Highlight(start=200, end=230, peak=210, score=5),
          Highlight(start=300, end=330, peak=310, score=4)]
    made = create_clips(Creator(), "v1", hs, limit=2, registry=reg)
    assert len(made) == 2 and all(h.twitch_clip for h in hs)


def test_vod_command_checks_vod_owner_not_channel_flag(tmp_path, monkeypatch):
    from twitch_shorts import pipeline, twitch_api

    seen = {}

    class FakeHelix:
        def __init__(self, *a, **k):
            pass

        def get_video(self, vid):
            return VideoInfo(vid, "7", "someone_else", "S", "t", datetime(2026, 10, 8, tzinfo=timezone.utc), 600, "u")

        def get_clips_for_video(self, info):
            return []

    monkeypatch.setattr(twitch_api, "HelixClient", FakeHelix)
    monkeypatch.setattr(pipeline, "process", lambda *a, **k: seen.update(k) or pipeline.RunResult([], tmp_path))
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n[twitch]\nclient_id = "c"\nclient_secret = "s"\n')
    (tmp_path / "work" / "vod_1234567").mkdir(parents=True)
    (tmp_path / "work" / "vod_1234567" / "chat.jsonl").write_text("", encoding="utf-8")
    cli.main(["-c", str(conf), "vod", "1234567", "--channel", "yuuki_ftw", "--twitch-clips", "--dry-run"])
    assert seen["channel"] == "yuuki_ftw" and seen["clip_owner"] == "someone_else"


def test_local_has_no_twitch_clips_flag():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["local", "a.mp4", "--twitch-clips"])


def test_config_hashtags_are_normalized():
    from twitch_shorts.pipeline import _fill_post_text

    cfg = Config()
    cfg.publish.hashtags = ["shorts", "Twitch切り抜き", " "]
    h = Highlight(start=0, end=30, peak=10, score=1, hashtags=["#Shorts"])
    _fill_post_text(cfg, h, "y")
    assert h.hashtags == ["#Shorts", "#Twitch切り抜き"]


def test_schedule_does_not_readd_removed_entries(tmp_path):
    from twitch_shorts.schedule import add_to_schedule, load_schedule

    cfg = Config(output_dir=str(tmp_path / "out"))
    old = Highlight(start=0, end=30, peak=5, score=1, title="old", output_path="/x/old.mp4")
    new = Highlight(start=50, end=80, peak=60, score=2, title="new", output_path="/x/new.mp4")
    add_to_schedule(cfg, [new], update_only=[old])
    assert [e["title"] for e in load_schedule(cfg)] == ["new"]



# --- 5 回目のレビュー指摘の回帰テスト ---------------------------------------

def test_ctrl_c_before_any_data_still_exits(tmp_path, monkeypatch):
    class Proc:
        returncode = None
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    class FakeChat:
        def __init__(self, *a):
            self.count = 0

        def start(self):
            pass

        def stop(self):
            pass

    def interrupt(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher, "start_live_recording", lambda *a: Proc())
    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")), "ch")


def test_sigterm_is_treated_like_ctrl_c():
    import signal

    old = signal.getsignal(signal.SIGTERM)
    try:
        watcher._stop_on_terminate()
        with pytest.raises(KeyboardInterrupt):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, old)


def test_leftover_recordings_are_processed_once(make_stream, tmp_path):
    import shutil

    video, chat_path = make_stream(duration=100, events=(50,))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    session = tmp_path / "work" / "yuuki_ftw" / "20261008_200000"
    session.mkdir(parents=True)
    import subprocess

    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c", "copy", "-f", "mpegts",
                    str(session / "stream.ts")], check=True)
    shutil.copy(chat_path, session / "chat.jsonl")
    watcher.process_leftovers(cfg, "yuuki_ftw")
    report = tmp_path / "out" / "yuuki_ftw" / "20261008_200000" / "highlights.json"
    assert report.exists()
    mtime = report.stat().st_mtime
    watcher.process_leftovers(cfg, "yuuki_ftw")  # 処理済みは再処理しない
    assert report.stat().st_mtime == mtime


def test_refresh_uses_token_updated_by_another_process(tmp_path):
    _token(tmp_path, expires_at=time.time() - 1)
    session = FakeSession(lambda *a: pytest.fail("別プロセスの更新済みトークンがあるのに更新した"))
    t = UserToken("cid", "", tmp_path, session=session)
    save_token(tmp_path, {**t.token, "access_token": "fresh", "refresh_token": "rt2",
                          "expires_at": time.time() + 9999})
    assert t.access_token() == "fresh"


def test_registry_skips_malformed_entries(tmp_path):
    from twitch_shorts.twitch_clips import _load_registry, create_clips

    reg = tmp_path / "twitch_clips.json"
    reg.write_text(json.dumps([{"vod_id": "v1", "end": None, "url": "x"}, {"vod_id": "v1", "end": 30},
                               {"vod_id": "v1", "end": "30", "url": "https://clips.twitch.tv/OK"}]), encoding="utf-8")
    assert [r["url"] for r in _load_registry(reg)] == ["https://clips.twitch.tv/OK"]
    h = Highlight(start=0, end=30, peak=10, score=1)
    create_clips(None, "v1", [h], registry=reg)
    assert h.twitch_clip == "https://clips.twitch.tv/OK"


def test_window_normalization_is_shared():
    from twitch_shorts.twitch_clips import normalize_window

    assert normalize_window(125.4, 80) == (126, 60.0)
    assert normalize_window(5.3, 5.3) == (6, 5.3)
    assert clip_window(Highlight(start=0, end=2, peak=1, score=1)) == normalize_window(5, 5)


def test_vod_owner_falls_back_to_channel_without_api(tmp_path, monkeypatch):
    from twitch_shorts import pipeline

    seen = {}
    monkeypatch.setattr(pipeline, "process", lambda *a, **k: seen.update(k) or pipeline.RunResult([], tmp_path))
    conf = tmp_path / "c.toml"
    conf.write_text(f'work_dir = "{tmp_path / "work"}"\n')
    (tmp_path / "work" / "vod_1234567").mkdir(parents=True)
    (tmp_path / "work" / "vod_1234567" / "chat.jsonl").write_text("", encoding="utf-8")
    cli.main(["-c", str(conf), "vod", "1234567", "--channel", "yuuki_ftw", "--twitch-clips", "--dry-run"])
    assert seen["clip_owner"] == "yuuki_ftw"


def test_recorder_keeps_ads_for_aligned_timeline(monkeypatch, tmp_path):
    from twitch_shorts import download

    captured = {}

    class P:
        def __init__(self, cmd, **k):
            captured["cmd"] = cmd

    monkeypatch.setattr(download.shutil, "which", lambda n: "/usr/bin/streamlink")
    monkeypatch.setattr(download.subprocess, "Popen", P)
    download.start_live_recording("yuuki_ftw", tmp_path / "s.ts", "streamlink")
    assert "--twitch-disable-ads" not in captured["cmd"]


def test_doctor_font_check_times_out(monkeypatch):
    import subprocess

    from twitch_shorts import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda n: "/usr/bin/" + n)

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

    monkeypatch.setattr(doctor.subprocess, "run", slow)
    assert doctor._japanese_font()[0] is None
