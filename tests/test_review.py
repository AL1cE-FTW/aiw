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
