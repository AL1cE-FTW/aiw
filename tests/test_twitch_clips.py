import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from twitch_shorts import cli, watcher
from twitch_shorts.config import Config
from twitch_shorts.models import Highlight
from twitch_shorts.twitch_api import VideoInfo
from twitch_shorts.twitch_auth import TwitchAuthError, UserToken, device_login, load_token, save_token
from twitch_shorts.twitch_clips import ClipCreator, ClipError, clip_window, create_clips


def _vod2(*a, **k):
    info = watcher.find_stream_vod_info(*a, **k)
    return info[:2] if info else None

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
    assert _vod2(FakeHelix(), "yuuki_ftw", rec_start) == ("cur", 95)
    # 録画開始より後に始まった VOD (別の配信) は使わない
    assert _vod2(FakeHelix(), "yuuki_ftw", started.timestamp() - 3600) is None


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
    assert _vod2(FakeHelix(), "yuuki_ftw", today) is None
    # 配信の開始時刻が分かれば、それと一致する VOD だけを使う
    assert _vod2(FakeHelix(), "yuuki_ftw", today, stream_started=today - 60) is None
    assert _vod2(FakeHelix(), "yuuki_ftw", yesterday.timestamp() + 30,
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

    from twitch_shorts import download, twitch_clips

    monkeypatch.setattr(download, "download_audio", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))

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

    # 配信は 30 秒前に始まった (VOD の位置の計算は現在時刻を使うので、固定の日時にしない)
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=30)

    class FakeHelix:
        def is_live(self, login):
            return False

        def get_stream(self, login):
            return {"viewer_count": 3, "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ")}

        def get_user_id(self, login):
            return "42"

        def get_recent_archives(self, uid, n):
            return [VideoInfo("v1", "42", "yuuki_ftw", "Y", "t", started, 999, "u")]

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
    assert _vod2(FakeHelix(), "yuuki_ftw", prev_start.timestamp() + 3600 + 300,
                                   user_id="42") is None


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

def test_final_pass_uses_vod_timeline(make_stream, tmp_path, monkeypatch):
    import shutil
    import subprocess

    from twitch_shorts import download

    monkeypatch.setattr(download, "download_audio", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))

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
        def is_live(self, login):
            return False

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
    assert lookups, "配信後の最終処理で VOD を探す"
    h = done[0]
    # 最終処理は VOD の時刻で行う: 録画の 50 秒目の山 + (録画開始 - 遅れ - 配信開始 = 292 秒)
    assert abs(h.peak - (50 + 300 - 8)) <= 5
    t = int(h.start)
    assert h.vod_url.endswith(f"t={t // 3600}h{t % 3600 // 60}m{t % 60}s")


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
        handler = signal.getsignal(signal.SIGTERM)
        handler(signal.SIGTERM, None)  # その場では中断せず、印を付けるだけ
        assert watcher._STOP.is_set()
        with pytest.raises(KeyboardInterrupt):
            watcher._sleep_checking_stop(5)  # 待機中なら Ctrl+C と同じく止まる
    finally:
        signal.signal(signal.SIGTERM, old)
        watcher._STOP.clear()


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
    watcher._save_parts(session, [(session / "stream.ts", 0.0)])
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


def test_recorder_skips_ads(monkeypatch, tmp_path):
    from twitch_shorts import download

    captured = {}

    class P:
        def __init__(self, cmd, **k):
            captured["cmd"] = cmd

    monkeypatch.setattr(download.shutil, "which", lambda n: "/usr/bin/streamlink")
    monkeypatch.setattr(download.subprocess, "Popen", P)
    download.start_live_recording("yuuki_ftw", tmp_path / "s.ts", "streamlink")
    # 広告の映像がショートにならないよう録画しない (配信後の最終処理は VOD から行うので位置はずれない)
    assert "--twitch-disable-ads" in captured["cmd"]


def test_doctor_font_check_times_out(monkeypatch):
    import subprocess

    from twitch_shorts import doctor

    monkeypatch.setattr(doctor.shutil, "which", lambda n: "/usr/bin/" + n)

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

    monkeypatch.setattr(doctor.subprocess, "run", slow)
    assert doctor._japanese_font()[0] is None



# --- 6 回目のレビュー指摘の回帰テスト ---------------------------------------

def _session(tmp_path, make_stream, name="20261008_200000"):
    import shutil
    import subprocess

    video, chat_path = make_stream(duration=100, events=(50,))
    session = tmp_path / "work" / "yuuki_ftw" / name
    session.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-c", "copy", "-f", "mpegts",
                    str(session / "stream.ts")], check=True)
    shutil.copy(chat_path, session / "chat.jsonl")
    watcher._save_parts(session, [(session / "stream.ts", 0.0)])
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    return cfg, session


def test_leftover_ignores_live_preview_and_marks_processed(make_stream, tmp_path):
    cfg, session = _session(tmp_path, make_stream)
    run_dir = tmp_path / "out" / "yuuki_ftw" / session.name
    (run_dir / "live").mkdir(parents=True)
    (run_dir / "live" / "highlights.json").write_text("{}", encoding="utf-8")  # 配信中のプレビューだけある
    watcher.process_leftovers(cfg, "yuuki_ftw")
    assert (session / "processed").exists()
    assert json.loads((run_dir / "highlights.json").read_text(encoding="utf-8"))["highlights"]


def test_leftover_gives_up_after_two_attempts(tmp_path, monkeypatch):
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    session = tmp_path / "work" / "yuuki_ftw" / "broken"
    session.mkdir(parents=True)
    (session / "stream.ts").write_bytes(b"not a video")
    watcher._save_parts(session, [(session / "stream.ts", 0.0)])
    runs = []
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: runs.append(1) or None)  # 失敗し続ける
    for _ in range(4):
        watcher.process_leftovers(cfg, "yuuki_ftw")
    assert len(runs) == 2


def test_final_pass_falls_back_to_recording_when_vod_fails(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 10.0, 1e6))
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a, **k: None)  # VOD のダウンロードに失敗
    out = watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session,
                              tmp_path / "out" / "yuuki_ftw" / session.name, False)
    assert out and out[0].output_path.endswith(".mp4")
    assert (session / "processed").exists()


def test_atomic_write_permissions_and_lock(tmp_path):
    import os
    import stat
    import threading

    from twitch_shorts.fileutil import file_lock, write_json_atomic

    p = tmp_path / "secret.json"
    (tmp_path / "secret.json.tmp").write_text("old", encoding="utf-8")  # 以前の残り
    write_json_atomic(p, {"a": 1}, private=True)
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    write_json_atomic(tmp_path / "pub.json", [1])
    assert json.loads((tmp_path / "pub.json").read_text()) == [1]

    counter = tmp_path / "count.json"
    write_json_atomic(counter, 0)

    def bump():
        for _ in range(20):
            with file_lock(counter):
                write_json_atomic(counter, json.loads(counter.read_text()) + 1)

    ts = [threading.Thread(target=bump) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert json.loads(counter.read_text()) == 80


def test_hashtag_normalization_handles_fullwidth():
    from twitch_shorts.models import normalize_hashtag

    assert [normalize_hashtag(t) for t in ["＃APEX", "#shorts", " clutch ", "#", ""]] == \
        ["#APEX", "#shorts", "#clutch", "", ""]


def test_auth_failure_during_batch_stops_with_login_hint(tmp_path, caplog):
    from twitch_shorts.twitch_clips import create_clips

    class Creator:
        def from_vod(self, *a, **k):
            raise TwitchAuthError("revoked")

    hs = [Highlight(start=i * 100, end=i * 100 + 30, peak=i * 100 + 10, score=1) for i in range(3)]
    assert create_clips(Creator(), "v1", hs) == []
    assert "twitch-shorts login をやり直して" in caplog.text



# --- 7 回目のレビュー指摘の回帰テスト ---------------------------------------

def test_failed_final_pass_is_not_marked_and_dry_run_never_marks(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    run_dir = tmp_path / "out" / "yuuki_ftw" / session.name
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: None)  # 処理に失敗
    watcher._final_pass(cfg, "yuuki_ftw", None, 0.0, None, session, run_dir, False)
    assert not (session / "processed").exists()
    monkeypatch.undo()
    watcher._final_pass(cfg, "yuuki_ftw", None, 0.0, None, session, run_dir, True)  # dry-run
    assert not (session / "processed").exists()
    watcher.process_leftovers(cfg, "yuuki_ftw", dry_run=True)
    assert not (session / "processed").exists() and not (session / "attempts").exists()


def test_legacy_sessions_with_output_are_not_reprocessed(tmp_path, monkeypatch):
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    session = tmp_path / "work" / "yuuki_ftw" / "old"
    session.mkdir(parents=True)
    (session / "stream.ts").write_bytes(b"x")
    run_dir = tmp_path / "out" / "yuuki_ftw" / "old"
    run_dir.mkdir(parents=True)
    (run_dir / "highlights.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: pytest.fail("処理済みの古い録画を処理した"))
    watcher.process_leftovers(cfg, "yuuki_ftw")


def test_one_failed_render_does_not_abort_the_rest(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import pipeline
    from twitch_shorts.chat import load_chat

    video, chat_path = make_stream(duration=150, events=(40, 110))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    real = pipeline.render_highlight
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("section download failed")
        return real(*a, **k)

    monkeypatch.setattr(pipeline, "render_highlight", flaky)
    r = pipeline.process(cfg, pipeline.LocalSource(str(video)), load_chat(chat_path), tmp_path / "o")
    assert [bool(h.output_path) for h in r.highlights] == [False, True]


def test_stop_request_is_seen_while_waiting():
    watcher._STOP.clear()
    watcher._STOP.set()
    with pytest.raises(KeyboardInterrupt):
        watcher._sleep_checking_stop(5)
    watcher._STOP.clear()



# --- 8 回目のレビュー指摘の回帰テスト ---------------------------------------

def test_recorder_restarts_in_same_session_while_still_live(tmp_path, monkeypatch):
    starts = []

    class Proc:
        returncode = 0
        stderr = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_rec(c, out, r, q):
        out.write_bytes(b"data")
        starts.append(out.name)
        return Proc()

    class FakeChat:
        def __init__(self, channel, out_path, *a):
            self.out_path, self.count = out_path, 0

        def start(self):
            self.out_path.write_text("", encoding="utf-8")

        def stop(self):
            pass

    lives = iter([True, False])  # 1 回目の終了時は配信中、2 回目は終了

    class Helix:
        def get_stream(self, login):
            return None

        def is_live(self, login):
            return next(lives)

    finals = []
    monkeypatch.setattr(watcher, "start_live_recording", fake_rec)
    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher, "_sleep_checking_stop", lambda s: None)
    monkeypatch.setattr(watcher, "_final_pass", lambda cfg, ch, h, st, ss, session_dir, *a, **k: finals.append(
        [p.name for p, _ in watcher._load_parts(session_dir)]) or [])
    watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")), "ch",
                               helix=Helix())
    assert starts == ["stream.ts", "stream_2.ts"]
    assert finals == [["stream.ts", "stream_2.ts"]]  # 最終処理は 1 回、両方の録画を対象に


def test_vod_fetch_failure_falls_back_to_recording(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import download

    cfg, session = _session(tmp_path, make_stream)
    monkeypatch.setattr(download, "download_audio", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(download, "download_section", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 0.0, 1e6))
    out = watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session,
                              tmp_path / "out" / "yuuki_ftw" / session.name, False)
    assert out and all(h.output_path.endswith(".mp4") for h in out)
    assert (session / "processed").exists()


def test_clips_only_for_rendered_shorts(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import pipeline, twitch_clips

    cfg, video, chat = _clip_setup(tmp_path, make_stream)
    monkeypatch.setattr(pipeline, "render_highlight", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(twitch_clips.ClipCreator, "from_vod", lambda *a, **k: pytest.fail("書き出せていない場面のクリップ"))
    pipeline.process(cfg, pipeline.LocalSource(str(video)), chat, tmp_path / "o", clip_vod=("v1", 0.0),
                     channel="yuuki_ftw")



# --- 9 回目のレビュー指摘の回帰テスト ---------------------------------------

def test_failed_session_with_partial_report_is_retried(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    watcher._save_parts(session, [(session / "stream.ts", 0.0)])
    run_dir = tmp_path / "out" / "yuuki_ftw" / session.name
    run_dir.mkdir(parents=True)
    (run_dir / "highlights.json").write_text("{}", encoding="utf-8")  # 途中まで書けて失敗した
    runs = []
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: runs.append(1) or [])
    watcher.process_leftovers(cfg, "yuuki_ftw")
    assert runs == [1] and (session / "processed").exists()


def test_interrupted_leftover_does_not_use_up_attempts(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)

    def killed(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher, "_run_from_recording", killed)
    with pytest.raises(KeyboardInterrupt):
        watcher.process_leftovers(cfg, "yuuki_ftw")
    assert not (session / "attempts").exists()


def test_vod_that_ends_before_the_session_is_not_used(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)  # 録画は 100 秒
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 50.0, 20.0))  # VOD は 20 秒で終わっている
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a, **k: pytest.fail("録画の途中で終わる VOD を使った"))
    out = watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session,
                              tmp_path / "out" / "yuuki_ftw" / session.name, False)
    assert out and out[0].output_path


def test_recorder_gives_up_after_quick_failures_and_waits_for_end(tmp_path, monkeypatch):
    starts = []

    class Proc:
        returncode = 0
        stderr = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def fake_rec(c, out, r, q):
        out.write_bytes(b"data")
        starts.append(out.name)
        return Proc()

    class FakeChat:
        def __init__(self, channel, out_path, *a):
            self.out_path, self.count = out_path, 0

        def start(self):
            self.out_path.write_text("", encoding="utf-8")

        def stop(self):
            pass

    waited = []
    monkeypatch.setattr(watcher, "start_live_recording", fake_rec)
    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher, "_still_live", lambda *a: True)
    monkeypatch.setattr(watcher, "_wait_until_offline", lambda *a: waited.append(1))
    monkeypatch.setattr(watcher, "_sleep_checking_stop", lambda s: None)
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: [])
    watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")), "ch",
                               helix=object())
    assert len(starts) == watcher.MAX_RECORDER_RESTARTS and waited == [1]


def test_file_lock_is_not_stolen_while_held(tmp_path):
    from twitch_shorts.fileutil import file_lock

    target = tmp_path / "x.json"
    with file_lock(target):
        with pytest.raises(TimeoutError):
            with file_lock(target, timeout=0.3):
                pass
    with file_lock(target, timeout=0.3):  # 解放後は取れる
        pass


def test_auto_has_no_twitch_clips_flag():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["auto", "ch", "--twitch-clips"])



# --- 10 回目のレビュー指摘の回帰テスト --------------------------------------

def test_truncated_chat_and_viewer_lines_are_skipped(tmp_path):
    from twitch_shorts.chat import load_chat
    from twitch_shorts.viewers import load_viewers

    c = tmp_path / "chat.jsonl"
    c.write_text('{"offset": 1, "user": "a", "text": "x"}\n{"offset": 2, "us', encoding="utf-8")
    assert [m.offset for m in load_chat(c)] == [1]
    v = tmp_path / "viewers.jsonl"
    v.write_text('{"offset": 1, "viewers": 3}\n{"offset": 2, "vie', encoding="utf-8")
    assert [x.viewers for x in load_viewers(v)] == [3]


def test_recording_pass_with_no_rendered_short_counts_as_failure(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import pipeline

    cfg, session = _session(tmp_path, make_stream)
    monkeypatch.setattr(pipeline, "render_highlight", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    out = watcher._final_pass(cfg, "yuuki_ftw", None, 0.0, None, session,
                              tmp_path / "out" / "yuuki_ftw" / session.name, False)
    assert out is None and not (session / "processed").exists()  # 失敗は None (次回やり直す)


def test_dry_run_sessions_are_not_processed_later(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    watcher._final_pass(cfg, "yuuki_ftw", None, 0.0, None, session,
                        tmp_path / "out" / "yuuki_ftw" / session.name, True)
    assert (session / "dry_run").exists()
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: pytest.fail("dry-run の録画を処理した"))
    watcher.process_leftovers(cfg, "yuuki_ftw")


def test_restart_failure_still_runs_final_pass(tmp_path, monkeypatch):
    class Proc:
        returncode = 0
        stderr = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    calls = []

    def rec(c, out, r, q):
        calls.append(out.name)
        if len(calls) > 1:
            raise FileNotFoundError("streamlink")
        out.write_bytes(b"data")
        return Proc()

    class FakeChat:
        def __init__(self, channel, out_path, *a):
            self.out_path, self.count = out_path, 0

        def start(self):
            self.out_path.write_text("", encoding="utf-8")

        def stop(self):
            pass

    finals = []
    monkeypatch.setattr(watcher, "start_live_recording", rec)
    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher, "_still_live", lambda *a: True)
    monkeypatch.setattr(watcher, "_sleep_checking_stop", lambda s: None)
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: finals.append(1) or [])
    watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")), "ch",
                               helix=object())
    assert finals == [1]


def test_vod_pass_uses_known_vod_duration(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import download

    cfg, session = _session(tmp_path, make_stream)
    seen = {}
    monkeypatch.setattr(download, "download_audio", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(watcher, "process", lambda *a, **k: seen.update(k) or (_ for _ in ()).throw(RuntimeError()))
    watcher._run_from_vod(cfg, "yuuki_ftw", ("v1", 0.0, 7200.0), session, tmp_path / "o", True)
    assert seen["duration"] == 7200.0


def test_leftover_lock_file_from_crash_does_not_block(tmp_path):
    from twitch_shorts.fileutil import file_lock

    target = tmp_path / "x.json"
    (tmp_path / "x.json.lock").write_text("123")  # 異常終了で残ったロックファイル (OS のロックは無い)
    with file_lock(target, timeout=1):
        pass


# --- 11 回目のレビュー指摘の回帰テスト --------------------------------------

class _Checker:
    def __init__(self, live, helix=None):
        self.live, self.helix = live, helix

    def is_live(self, channel, default=False):
        return self.live


def test_leftovers_wait_while_live(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: pytest.fail("配信中に処理した"))
    watcher.process_leftovers(cfg, "yuuki_ftw", checker=_Checker(True))


def test_leftover_that_finished_vod_pass_is_only_marked(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    watcher._save_meta(session, start_time=1000.0, stream_started=900.0, vod_id="v1")
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: pytest.fail("作り終えた VOD をもう一度作った"))
    watcher.process_leftovers(cfg, "yuuki_ftw", checker=_Checker(False, helix=object()))
    assert (session / "processed").exists()


def test_leftover_sharing_vod_with_another_session_still_uses_its_chat(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    watcher._save_meta(session, start_time=1000.0, stream_started=900.0)
    other = session.parent / "20261008_210000"
    other.mkdir()
    watcher._save_meta(other, vod_id="v1")
    (other / "processed").write_text("x")
    calls = []
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: calls.append(a[5]) or [])
    watcher.process_leftovers(cfg, "yuuki_ftw", checker=_Checker(False, helix=object()))
    assert calls == [session] and (session / "processed").exists()


def test_leftover_uses_vod_first_when_meta_known(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    watcher._save_meta(session, start_time=1000.0, stream_started=900.0)
    calls = []
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: None)
    monkeypatch.setattr(watcher, "_final_pass", lambda cfg, ch, h, st, ss, *a, **k: calls.append((st, ss)) or [])
    watcher.process_leftovers(cfg, "yuuki_ftw", checker=_Checker(False, helix=object()))
    assert calls == [(1000.0, 900.0)] and (session / "processed").exists()


def test_tiny_failing_part_does_not_fail_the_session(make_stream, tmp_path):
    cfg, session = _session(tmp_path, make_stream)
    (session / "stream_2.ts").write_bytes(b"\x00" * 100)  # 配信終了間際に録り直してすぐ終わった分
    watcher._save_parts(session, [(session / "stream.ts", 0.0), (session / "stream_2.ts", 100.0)])
    out = watcher._run_from_recording(cfg, "yuuki_ftw", session, tmp_path / "o", False)
    assert out and out[0].output_path


def test_stop_before_restarting_recorder(tmp_path, monkeypatch):
    class Proc:
        returncode = 0
        stderr = None

        def poll(self):
            watcher._STOP.set()  # 録画中に停止の指示が来て、同時に録画も終わった
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    starts = []

    def rec(c, out, r, q):
        starts.append(out.name)
        out.write_bytes(b"data")
        return Proc()

    class FakeChat:
        def __init__(self, channel, out_path, *a):
            self.out_path, self.count = out_path, 0

        def start(self):
            self.out_path.write_text("", encoding="utf-8")

        def stop(self):
            pass

    monkeypatch.setattr(watcher, "start_live_recording", rec)
    monkeypatch.setattr(watcher, "LiveChatRecorder", FakeChat)
    monkeypatch.setattr(watcher, "_still_live", lambda *a: True)
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: [])
    try:
        with pytest.raises(KeyboardInterrupt):
            watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")),
                                       "ch", helix=object())
    finally:
        watcher._STOP.clear()
    assert starts == ["stream.ts"]


# --- 12 回目のレビュー指摘の回帰テスト --------------------------------------

def test_dry_run_leftover_does_not_mark_real_session(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    watcher._save_meta(session, start_time=1000.0)
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: [])
    watcher.process_leftovers(cfg, "yuuki_ftw", dry_run=True)
    assert not (session / "dry_run").exists() and not (session / "processed").exists()


def test_final_pass_excludes_shorts_made_from_same_vod(make_stream, tmp_path, monkeypatch):
    import json as _json

    cfg, session = _session(tmp_path, make_stream)
    other = session.parent / "20261008_190000"
    other.mkdir()
    watcher._save_meta(other, vod_id="v1")
    out_other = tmp_path / "out" / "yuuki_ftw" / other.name
    out_other.mkdir(parents=True)
    out_other.joinpath("highlights.json").write_text(_json.dumps({"highlights": [
        {"start": 10.0, "end": 40.0, "peak": 20.0, "score": 1.0, "output_path": "a.mp4"}]}), encoding="utf-8")
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 0.0, 9999.0))
    seen = []
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a: seen.append(a[-2]) or [])
    out = watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session, tmp_path / "o", False)
    assert out == [] and [h.start for h in seen[0]] == [10.0]


def test_final_pass_skips_vod_done_by_latest(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    (tmp_path / "work" / "processed_vods.json").write_text('["v1"]', encoding="utf-8")
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 0.0, 9999.0))
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a, **k: pytest.fail("latest で作成済みの VOD を作った"))
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: pytest.fail("録画から作った"))
    assert watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session, tmp_path / "o", False) == []
    assert (session / "processed").exists()


def test_failing_to_save_vod_id_does_not_crash(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 0.0, 9999.0))
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a, **k: [])

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(watcher, "_save_meta", boom)
    assert watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session, tmp_path / "o", False) == []


def test_leftover_without_stream_start_does_not_use_current_stream(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    seen = []
    monkeypatch.setattr(watcher, "_stream_started_at", lambda *a: pytest.fail("いまの配信の開始時刻を使った"))
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda h, c, rs, ss=None: seen.append(ss))
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: [])
    watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, None, session, tmp_path / "o", False, leftover=True)
    assert seen == [None]


def test_legacy_session_without_parts_is_never_reprocessed(tmp_path, monkeypatch):
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    session = tmp_path / "work" / "yuuki_ftw" / "old"
    session.mkdir(parents=True)
    (session / "stream.ts").write_bytes(b"x")  # 結果のフォルダーは消されている
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: pytest.fail("古い録画を処理した"))
    watcher.process_leftovers(cfg, "yuuki_ftw")


def test_still_live_uses_checker_without_api():
    assert watcher._still_live(None, "ch", _Checker(True)) is True


def test_leftovers_check_live_only_for_candidates(tmp_path):
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    for name in ("a", "b", "c"):
        d = tmp_path / "work" / "yuuki_ftw" / name
        d.mkdir(parents=True)
        (d / "processed").write_text("x")

    class Counting(_Checker):
        n = 0

        def is_live(self, channel, default=False):
            Counting.n += 1
            return False

    watcher.process_leftovers(cfg, "yuuki_ftw", checker=Counting(False))
    assert Counting.n == 0


def test_vod_work_dir_is_removed(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    work = tmp_path / "work" / "vod_v1" / f"auto_{session.name}"

    def fake_process(*a, **k):
        work.mkdir(parents=True, exist_ok=True)
        (work / "audio.wav").write_bytes(b"x")
        raise RuntimeError("download failed")

    monkeypatch.setattr(watcher, "process", fake_process)
    assert watcher._run_from_vod(cfg, "yuuki_ftw", ("v1", 0.0, 100.0), session, tmp_path / "o", False) is None
    assert not work.exists()


# --- 13 回目のレビュー指摘の回帰テスト --------------------------------------

def test_completed_vod_is_recorded_for_latest(make_stream, tmp_path, monkeypatch):
    from twitch_shorts.fileutil import processed_vods

    cfg, session = _session(tmp_path, make_stream)
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 0.0, 9999.0))
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a, **k: [])
    watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session, tmp_path / "o", False, vod_complete=False)
    assert processed_vods(cfg.work_dir) == set()  # 配信の途中で止めた分は、残りを latest で作れるように
    watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session, tmp_path / "o", False)
    assert processed_vods(cfg.work_dir) == {"v1"}


def test_highlights_of_vod_are_deduplicated(tmp_path):
    import json as _json

    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    a = {"start": 10.0, "end": 40.0, "peak": 20.0, "score": 1.0, "output_path": "a.mp4"}
    b = {"start": 60.0, "end": 90.0, "peak": 70.0, "score": 1.0, "output_path": "b.mp4"}
    for name, hs in (("s1", [a]), ("s2", [a, b])):  # s2 の結果には s1 の分も入っている
        d = tmp_path / "work" / "yuuki_ftw" / name
        d.mkdir(parents=True)
        watcher._save_meta(d, vod_id="v1")
        out = tmp_path / "out" / "yuuki_ftw" / name
        out.mkdir(parents=True)
        (out / "highlights.json").write_text(_json.dumps({"highlights": hs}), encoding="utf-8")
    me = tmp_path / "work" / "yuuki_ftw" / "s3"
    me.mkdir()
    found = watcher._highlights_of_vod(cfg, "yuuki_ftw", "v1", me)
    assert sorted(h.output_path for h in found) == ["a.mp4", "b.mp4"]


def test_hashtags_with_spaces_are_joined():
    from twitch_shorts.models import normalize_hashtag

    assert normalize_hashtag(" ＃Apex Legends ") == "#ApexLegends"
    assert normalize_hashtag("  ") == ""


def test_chat_lines_with_bad_offset_are_skipped(tmp_path):
    from twitch_shorts.chat import load_chat

    p = tmp_path / "chat.jsonl"
    p.write_text('{"offset": 1, "user": "a", "text": "x"}\n{"offset": null, "user": "b", "text": "y"}\n'
                 '{"offset": [1], "user": "c", "text": "z"}\n', encoding="utf-8")
    assert [m.user for m in load_chat(p)] == ["a"]


def test_unexpected_clip_error_does_not_stop_the_batch():
    from twitch_shorts.models import Highlight
    from twitch_shorts.twitch_clips import create_clips

    class Creator:
        n = 0

        def from_vod(self, vod_id, end, duration, title):
            Creator.n += 1
            if Creator.n == 1:
                raise TypeError("odd header")
            return {"id": "C2", "edit_url": "e"}

    hs = [Highlight(start=10, end=40, peak=20, score=2.0), Highlight(start=100, end=130, peak=110, score=1.0)]
    create_clips(Creator(), "v1", hs)
    assert Creator.n == 2 and hs[1].twitch_clip


def test_restarted_part_is_listed_before_recording(tmp_path, monkeypatch):
    class Proc:
        returncode = 0
        stderr = None

        def __init__(self, n):
            self.n = n

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    listed = []
    session_holder = []

    def fake_start(channel, path, *a):
        session_holder.append(path.parent)
        if path.name != "stream.ts":
            listed.append([d["file"] for d in json.loads((path.parent / "parts.json").read_text())])
            raise RuntimeError("recorder missing")
        path.write_bytes(b"x")
        return Proc(len(session_holder))

    monkeypatch.setattr(watcher, "start_live_recording", fake_start)
    monkeypatch.setattr(watcher, "LiveChatRecorder", lambda *a, **k: type("C", (), {
        "start": lambda s: None, "stop": lambda s: None, "count": 0})())
    monkeypatch.setattr(watcher, "_still_live", lambda *a: True)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: [])
    watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")), "ch")
    assert listed and "stream_2.ts" in listed[0]


# --- 14 回目のレビュー指摘の回帰テスト --------------------------------------

def test_latest_skips_vod_of_ongoing_stream(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from twitch_shorts import cli

    v = SimpleNamespace(id="v1", created_at=datetime(2026, 10, 8, 20, tzinfo=timezone.utc), title="t")
    assert cli._is_current_stream(v, {"started_at": "2026-10-08T20:00:30Z"})
    assert not cli._is_current_stream(v, {"started_at": "2026-10-09T20:00:00Z"})


def test_vod_choice_prefers_closest_start():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    t = datetime(2026, 10, 8, 20, tzinfo=timezone.utc).timestamp()
    newer = SimpleNamespace(id="new", created_at=datetime.fromtimestamp(t + 360, timezone.utc), duration=3600)
    older = SimpleNamespace(id="old", created_at=datetime.fromtimestamp(t, timezone.utc), duration=240)
    helix = SimpleNamespace(get_user_id=lambda c: "u", get_recent_archives=lambda u, n: [newer, older])
    assert watcher.find_stream_vod_info(helix, "ch", t + 10, t)[0] == "old"


def test_shared_vod_is_limited_to_this_recording(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 500.0, 9999.0))
    from twitch_shorts.models import Highlight
    monkeypatch.setattr(watcher, "_highlights_of_vod",
                        lambda *a: [Highlight(start=10, end=40, peak=20, score=1.0, output_path="a.mp4")])
    seen = []
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a: seen.append(a[-1]) or [])
    watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session, tmp_path / "o", False)
    start, end = seen[0]
    assert start == 500.0 and 590 <= end <= 610  # 録画は約 100 秒


def test_live_check_error_counts_as_live_for_leftovers(make_stream, tmp_path, monkeypatch):
    cfg, session = _session(tmp_path, make_stream)
    checker = watcher.LiveChecker(cfg)

    def boom(channel):
        raise RuntimeError("timeout")

    monkeypatch.setattr(watcher, "is_live_via_ytdlp", boom)
    monkeypatch.setattr(watcher, "_run_from_recording", lambda *a, **k: pytest.fail("配信中かもしれないのに処理した"))
    watcher.process_leftovers(cfg, "yuuki_ftw", checker=checker)


def test_recorder_messages_go_to_a_file(tmp_path, monkeypatch):
    import subprocess

    from twitch_shorts import download

    monkeypatch.setattr(download.shutil, "which", lambda n: None)
    seen = {}

    class P:
        def __init__(self, cmd, stdout=None, stderr=None):
            seen["stderr"] = stderr

    monkeypatch.setattr(download.subprocess, "Popen", P)
    proc = download.start_live_recording("ch", tmp_path / "stream.ts", "yt-dlp")
    assert seen["stderr"] is not subprocess.PIPE and proc.log_path == tmp_path / "stream.log"
    proc.log_path.write_bytes(b"error: 403")
    assert download.recorder_log_tail(proc) == "error: 403"


# --- 15 回目のレビュー指摘の回帰テスト --------------------------------------

def test_own_session_vod_in_processed_list_is_not_skipped(make_stream, tmp_path, monkeypatch):
    from twitch_shorts.fileutil import add_processed_vod

    cfg, session = _session(tmp_path, make_stream)
    other = session.parent / "20261008_190000"
    other.mkdir()
    watcher._save_meta(other, vod_id="v1")  # 前のセッションは VOD から作ったが 0 本だった
    add_processed_vod(cfg.work_dir, "v1")
    monkeypatch.setattr(watcher, "find_stream_vod_info", lambda *a, **k: ("v1", 0.0, 9999.0))
    calls = []
    monkeypatch.setattr(watcher, "_run_from_vod", lambda *a: calls.append(1) or [])
    watcher._final_pass(cfg, "yuuki_ftw", object(), 0.0, 0.0, session, tmp_path / "o", False)
    assert calls == [1]


def test_live_check_error_is_not_treated_as_stream_end():
    class Helix:
        def is_live(self, channel):
            raise RuntimeError("timeout")

    assert watcher._still_live(Helix(), "ch") is True


def test_clip_registry_is_rechecked_under_lock(tmp_path):
    import json as _json

    from twitch_shorts.models import Highlight
    from twitch_shorts.twitch_clips import clip_window, create_clips

    reg = tmp_path / "clips.json"
    h = Highlight(start=10, end=40, peak=20, score=1.0)
    end, dur = clip_window(h, 0.0)

    class Creator:
        def from_vod(self, *a):
            pytest.fail("別のプロセスが作ったクリップをもう一度作った")

    # 最初の読み込みの後に、別のプロセスが同じ場面のクリップを記録した状況
    reg.write_text("[]", encoding="utf-8")
    import twitch_shorts.twitch_clips as tc
    real = tc._load_registry
    calls = []

    def load(path, repair=False):
        calls.append(1)
        if len(calls) == 2:
            path.write_text(_json.dumps([{"vod_id": "v1", "end": round(end, 1), "duration": dur,
                                          "url": "https://clips.twitch.tv/X"}]), encoding="utf-8")
        return real(path, repair)

    tc._load_registry, saved = load, tc._load_registry
    try:
        create_clips(Creator(), "v1", [h], registry=reg)
    finally:
        tc._load_registry = saved
    assert h.twitch_clip == "https://clips.twitch.tv/X"


def test_llm_hashtags_as_string_are_split():
    from types import SimpleNamespace

    from twitch_shorts.config import LLMConfig
    from twitch_shorts.llm import rerank_with_claude
    from twitch_shorts.models import Highlight

    import json as _json
    text = _json.dumps({"candidates": [{"id": 0, "score": 8, "title": "t", "hook": "h", "category": "その他",
                                        "description": "d", "hashtags": "#apex #clutch", "reason": "r",
                                        "start": 10, "end": 40}]})
    resp = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **k: resp)))
    out = rerank_with_claude([Highlight(start=10, end=40, peak=20, score=1.0)], LLMConfig(), "ch", "", 15, 60,
                             client=client)
    assert out[0].hashtags == ["#apex", "#clutch"]


# --- 16 回目のレビュー指摘の回帰テスト --------------------------------------

def test_empty_recording_still_uses_vod(tmp_path, monkeypatch):
    class Proc:
        returncode = 1
        stderr = None

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

        def terminate(self):
            pass

    monkeypatch.setattr(watcher, "start_live_recording", lambda c, path, *a: Proc())  # 何も録れない
    monkeypatch.setattr(watcher, "LiveChatRecorder", lambda *a, **k: type("C", (), {
        "start": lambda s: None, "stop": lambda s: None, "count": 0})())
    monkeypatch.setattr(watcher, "ViewerRecorder", lambda *a, **k: type("V", (), {
        "start": lambda s: None, "stop": lambda s: None})())
    monkeypatch.setattr(watcher, "_still_live", lambda *a: False)
    monkeypatch.setattr(watcher, "_stream_started_at", lambda *a: None)
    finals = []
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: finals.append(1) or [])
    watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")), "ch",
                               helix=object())
    assert finals == [1]


def test_ytdlp_network_error_is_not_offline(monkeypatch):
    from yt_dlp.utils import DownloadError

    from twitch_shorts import download

    class Y:
        def __init__(self, msg):
            self.msg = msg

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, *a, **k):
            raise DownloadError(self.msg)

    monkeypatch.setattr(download, "_ydl", lambda opts: Y("ERROR: [twitch:stream] yuuki_ftw: The channel is not currently live"))
    assert download.is_live_via_ytdlp("yuuki_ftw") is False
    monkeypatch.setattr(download, "_ydl", lambda opts: Y("ERROR: Unable to download webpage: timed out"))
    with pytest.raises(DownloadError):
        download.is_live_via_ytdlp("yuuki_ftw")


def test_broken_clip_registry_is_kept_aside(tmp_path):
    from twitch_shorts.twitch_clips import _load_registry

    reg = tmp_path / "twitch_clips.json"
    reg.write_text("{broken", encoding="utf-8")
    assert _load_registry(reg) == [] and reg.exists()  # ロックを持たない読み込みでは動かさない
    assert _load_registry(reg, repair=True) == []
    assert not reg.exists() and list(tmp_path.glob("twitch_clips.json.broken-*"))


def test_registry_lock_timeout_does_not_crash(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from twitch_shorts import twitch_clips
    from twitch_shorts.models import Highlight

    @contextmanager
    def busy(path, timeout=120.0):
        raise TimeoutError("busy")
        yield

    monkeypatch.setattr(twitch_clips, "file_lock", busy)

    class Creator:
        def from_vod(self, *a):
            pytest.fail("ロックを取れないのに作った")

    out = twitch_clips.create_clips(Creator(), "v1", [Highlight(start=10, end=40, peak=20, score=1.0)],
                                    registry=tmp_path / "r.json")
    assert out == []


def test_written_json_follows_umask(tmp_path, monkeypatch):
    import stat

    from twitch_shorts import fileutil

    monkeypatch.setattr(fileutil, "_UMASK", 0o077)
    fileutil.write_json_atomic(tmp_path / "a.json", [])
    assert stat.S_IMODE((tmp_path / "a.json").stat().st_mode) == 0o600


# --- 17 回目のレビュー指摘の回帰テスト --------------------------------------

def test_failed_preview_renders_are_not_remembered(make_stream, tmp_path, monkeypatch):
    from twitch_shorts.models import Highlight

    cfg, session = _session(tmp_path, make_stream)
    ok = Highlight(start=10, end=40, peak=20, score=1.0, output_path="a.mp4")
    failed = Highlight(start=50, end=80, peak=60, score=1.0)
    monkeypatch.setattr(watcher, "_process_recording", lambda *a, **k: [ok, failed])
    out = watcher._preview(cfg, "yuuki_ftw", session, (session / "stream.ts", 0.0), tmp_path / "o", [], 200, False)
    assert out == [ok]


def test_offline_wait_is_bounded(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(watcher.time, "time", lambda: clock[0])
    monkeypatch.setattr(watcher, "_still_live", lambda *a: True)

    def tick(s):
        clock[0] += s

    monkeypatch.setattr(watcher, "_sleep_checking_stop", tick)
    watcher._wait_until_offline(Config(), None, "ch")
    assert clock[0] >= watcher.MAX_OFFLINE_WAIT


# --- 18 回目のレビュー指摘の回帰テスト --------------------------------------

def test_registry_with_bad_encoding_is_repaired(tmp_path):
    from twitch_shorts.twitch_clips import _load_registry, clips_made_for

    reg = tmp_path / "twitch_clips.json"
    reg.write_bytes(b'[{"vod_id": "v1", "url": "\xe3\x81"}]')
    assert clips_made_for(reg, "v1") == 0  # ロック無しの読み込みでは落ちない
    assert _load_registry(reg, repair=True) == [] and not reg.exists()


def test_corrupt_processed_vods_is_kept_aside(tmp_path):
    from twitch_shorts.fileutil import add_processed_vod, processed_vods

    (tmp_path / "processed_vods.json").write_text('["v1", "v2"', encoding="utf-8")
    add_processed_vod(tmp_path, "v3")
    assert processed_vods(tmp_path) == {"v3"}
    assert list(tmp_path.glob("processed_vods.json.broken-*"))


def test_registry_keeps_unknown_rows_and_survives_write_error(tmp_path, monkeypatch):
    import json as _json

    from twitch_shorts import twitch_clips
    from twitch_shorts.models import Highlight

    reg = tmp_path / "r.json"
    reg.write_text(_json.dumps([{"note": "手で足した行"}]), encoding="utf-8")

    class Creator:
        def from_vod(self, *a):
            return {"id": "C1", "edit_url": "e"}

    hs = [Highlight(start=10, end=40, peak=20, score=2.0), Highlight(start=100, end=130, peak=110, score=1.0)]
    twitch_clips.create_clips(Creator(), "v1", hs[:1], registry=reg)
    rows = _json.loads(reg.read_text(encoding="utf-8"))
    assert rows[0] == {"note": "手で足した行"} and len(rows) == 2

    def fail(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(twitch_clips, "write_json_atomic", fail)
    made = twitch_clips.create_clips(Creator(), "v1", hs[1:], registry=reg)
    assert made and hs[1].twitch_clip


# --- 19 回目のレビュー指摘の回帰テスト --------------------------------------

def test_token_with_bad_bytes_is_reported_not_crashing(tmp_path):
    from twitch_shorts.twitch_auth import load_token, token_path

    p = token_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'{"access_token": "\xff\xfe"}')
    assert load_token(tmp_path) is None


def test_processed_vods_with_bom_is_kept(tmp_path):
    from twitch_shorts.fileutil import add_processed_vod, processed_vods

    (tmp_path / "processed_vods.json").write_text('["v1"]', encoding="utf-8-sig")
    assert processed_vods(tmp_path) == {"v1"}
    add_processed_vod(tmp_path, "v2")
    assert processed_vods(tmp_path) == {"v1", "v2"} and not list(tmp_path.glob("*.broken-*"))


def test_stream_end_is_not_rechecked_for_vod_complete(tmp_path, monkeypatch):
    class Proc:
        returncode = 0
        stderr = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

    def fake_start(channel, path, *a):
        path.write_bytes(b"x")
        return Proc()

    answers = iter([False, True, True])  # 1 回目で終了を確認。2 回目以降は API の揺れで「配信中」
    monkeypatch.setattr(watcher, "start_live_recording", fake_start)
    monkeypatch.setattr(watcher, "LiveChatRecorder", lambda *a, **k: type("C", (), {
        "start": lambda s: None, "stop": lambda s: None, "count": 0})())
    monkeypatch.setattr(watcher, "_still_live", lambda *a: next(answers))
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    seen = []
    monkeypatch.setattr(watcher, "_final_pass", lambda *a, **k: seen.append(k.get("vod_complete")) or [])
    watcher.record_and_process(Config(output_dir=str(tmp_path / "o"), work_dir=str(tmp_path / "w")), "ch")
    assert seen == [True]


def test_schedule_csv_is_written(tmp_path):
    from twitch_shorts.models import Highlight
    from twitch_shorts.schedule import add_to_schedule

    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    h = Highlight(start=10, end=40, peak=20, score=1.0, title="テスト", output_path=str(tmp_path / "a.mp4"))
    add_to_schedule(cfg, [h], "ch")
    text = (tmp_path / "out" / "schedule.csv").read_text(encoding="utf-8-sig")
    assert text.startswith("publish_at,") and "テスト" in text
    assert not list((tmp_path / "out").glob("*.tmp"))
