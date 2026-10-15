import json
import re

import numpy as np

from twitch_shorts.config import Config
from twitch_shorts.models import Highlight
from twitch_shorts.pipeline import _fill_post_text, vod_timestamp_url
from twitch_shorts.review import _downsample, write_review_page


def test_review_page_embeds_highlights_safely(tmp_path):
    hs = [Highlight(start=10, end=40, peak=30, score=7.5, title="</script><b>危険</b>", category="面白い",
                    hashtags=["#shorts"], output_path=str(tmp_path / "short_01.mp4"),
                    vod_url="https://www.twitch.tv/videos/1?t=0h0m10s", twitch_clip="https://clips.twitch.tv/x")]
    page = write_review_page(tmp_path, hs, np.arange(5000, dtype=float), 5000, "yuuki_ftw", "配信").read_text("utf-8")
    assert "</script><b>" not in page  # データ中の </script> でページが壊れない
    data = json.loads(re.search(r"const DATA = (.*?);\n", page).group(1).replace("<\\/", "</"))
    [item] = data["items"]
    assert item["video"] == "short_01.mp4" and item["category"] == "面白い"
    assert item["vod_url"].endswith("t=0h0m10s") and item["twitch_clip"]
    assert len(data["score"]) == 600 and data["score"][-1] == 4999


def test_downsample_keeps_short_series():
    assert _downsample(np.array([1.0, 2.0])) == [1.0, 2.0]
    assert _downsample(np.array([])) == []


def test_post_text_defaults_and_vod_url():
    cfg = Config()
    h = Highlight(start=0, end=30, peak=10, score=1, hashtags=["#APEX", "#Shorts"])
    _fill_post_text(cfg, h, "yuuki_ftw")
    assert h.hashtags == ["#APEX", "#Shorts", "#Twitch切り抜き"]  # 大文字小文字違いの重複は足さない
    assert "yuuki_ftw" in h.description
    assert vod_timestamp_url("123", 3725.9) == "https://www.twitch.tv/videos/123?t=1h2m5s"



def test_review_page_survives_html_comment_and_tiny_series(tmp_path):
    hs = [Highlight(start=0, end=1, peak=0, score=1, title="t", chat_sample=["<!--<script>", "a & b"])]
    page = write_review_page(tmp_path, hs, np.array([1.0]), 1.0, "y", "").read_text("utf-8")
    body = page.split("const DATA = ", 1)[1].split(";\n", 1)[0]
    assert "<" not in body and ">" not in body and "&" not in body
    assert json.loads(body)["items"][0]["chat"] == ["<!--<script>", "a & b"]
    assert "pts.length < 2" in page


def test_portal_lists_every_run_with_publish_date(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from twitch_shorts.review import write_portal
    from twitch_shorts.schedule import add_to_schedule

    cfg = Config(output_dir=str(tmp_path / "out"))
    hs = []
    for run, title in (("a_1", "配信A"), ("b_2", "")):
        h = Highlight(start=0, end=30, peak=10, score=5, title="</script>", output_path=str(tmp_path / "out" / run / "s.mp4"))
        (tmp_path / "out" / run).mkdir(parents=True)
        (tmp_path / "out" / run / "highlights.json").write_text(
            json.dumps({"stream_title": title, "highlights": [h.to_dict()]}), encoding="utf-8")
        hs.append(h)
    add_to_schedule(cfg, hs[:1], now=datetime(2026, 10, 6, 9, 0, tzinfo=ZoneInfo("Asia/Tokyo")))
    page = write_portal(cfg).read_text(encoding="utf-8")
    items = json.loads(re.search(r"const ITEMS = (.*);\n", page).group(1))
    assert [(i["stream"], i["video"], i["publish_at"][:10]) for i in items] == [
        ("配信A", "a_1/s.mp4", "2026-10-06"), ("b_2", "b_2/s.mp4", "")]
    assert "</script>\"" not in page

def test_portal_finds_auto_runs_under_channel_folder(tmp_path):
    from twitch_shorts.review import write_portal

    cfg = Config(output_dir=str(tmp_path / "out"))
    for run in ("yuuki_ftw/20261013_200000", "yuuki_ftw/20261013_200000/live/rec_000"):
        d = tmp_path / "out" / run
        d.mkdir(parents=True)
        h = Highlight(start=0, end=30, peak=10, score=5, title="t", output_path=str(d / "s.mp4"))
        (d / "highlights.json").write_text(json.dumps({"stream_title": "配信", "highlights": [h.to_dict()]}),
                                           encoding="utf-8")
    page = write_portal(cfg).read_text(encoding="utf-8")
    items = json.loads(re.search(r"const ITEMS = (.*);\n", page).group(1))
    # auto の書き出し先 (チャンネル名/日時) も載せ、配信中のプレビューは載せない
    assert [i["video"] for i in items] == ["yuuki_ftw/20261013_200000/s.mp4"]
    assert items[0]["page"] == "yuuki_ftw/20261013_200000/index.html"
