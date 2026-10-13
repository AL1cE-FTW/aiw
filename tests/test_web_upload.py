from types import SimpleNamespace

from twitch_shorts.config import Config
from twitch_shorts.models import Highlight
from twitch_shorts.web_upload import build_payload, run_id_for, upload_run


def _h(start, end, score=1.0, **kw):
    return Highlight(start=start, end=end, peak=(start + end) / 2, score=score, **kw)


def test_payload_uses_vod_time_and_twitch_clip_limits():
    short = _h(100, 190, 3.0, title="神プレイ", hook="まさかの")  # 90 秒 → クリップは 60 秒に収める
    extra = _h(400, 420, 1.0, title="次点")
    p = build_payload("123-run", "yuuki_ftw", "配信", "123", 10.0, [short, extra], [short])
    a, b = p["items"]
    assert (a["start"], a["end"], a["short"]) == (110.0, 200.0, True)
    assert a["clip_duration"] == 60 and a["clip_end"] <= 200
    assert (b["short"], b["clip_end"], b["clip_duration"]) == (False, 430, 20.0)
    assert p["vod_id"] == "123" and p["id"] == "123-run"


def test_run_id_is_safe():
    assert run_id_for("123", "a b/../c") == "123-a_b____c"


def test_upload_needs_url_and_token_and_never_raises():
    cfg = Config()
    assert upload_run(cfg, {}) is False  # 設定が無ければ送らない
    cfg.web.url, cfg.web.upload_token = "https://aiw.example.workers.dev/", "tok"
    sent = {}

    class S:
        def post(self, url, json=None, timeout=None, headers=None):
            sent.update(url=url, headers=headers, json=json)
            return SimpleNamespace(status_code=200, text="")

    assert upload_run(cfg, {"id": "x"}, session=S()) is True
    assert sent["url"] == "https://aiw.example.workers.dev/api/upload"
    assert sent["headers"]["Authorization"] == "Bearer tok"

    import requests

    class Broken:
        def post(self, *a, **k):
            raise requests.ConnectionError("offline")

    assert upload_run(cfg, {"id": "x"}, session=Broken()) is False


def test_pipeline_sends_shorts_and_extra_candidates(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import pipeline, web_upload
    from twitch_shorts.chat import load_chat

    video, chat_path = make_stream(duration=200, events=(40, 110, 170))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    cfg.detect.top_n = 1
    cfg.web.url, cfg.web.upload_token = "https://aiw.example.workers.dev", "tok"
    got = []
    monkeypatch.setattr(web_upload, "upload_run", lambda c, payload: got.append(payload) or True)
    pipeline.process(cfg, pipeline.LocalSource(str(video)), load_chat(chat_path), tmp_path / "o",
                     clip_vod=("999", 0.0), channel="yuuki_ftw")
    [payload] = got
    assert payload["vod_id"] == "999"
    shorts = [i for i in payload["items"] if i["short"]]
    assert len(shorts) == 1 and len(payload["items"]) > 1  # ショートにした 1 本 + 次点の候補
    assert all(i["title"] for i in payload["items"])


def test_pipeline_does_not_send_without_vod_or_in_dry_run(make_stream, tmp_path, monkeypatch):
    from twitch_shorts import pipeline, web_upload
    from twitch_shorts.chat import load_chat

    video, chat_path = make_stream(duration=120, events=(50,))
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    cfg.web.url, cfg.web.upload_token = "https://aiw.example.workers.dev", "tok"
    monkeypatch.setattr(web_upload, "upload_run", lambda *a: (_ for _ in ()).throw(AssertionError("sent")))
    pipeline.process(cfg, pipeline.LocalSource(str(video)), load_chat(chat_path), tmp_path / "o1")
    pipeline.process(cfg, pipeline.LocalSource(str(video)), load_chat(chat_path), tmp_path / "o2",
                     clip_vod=("999", 0.0), dry_run=True)
