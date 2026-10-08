import json
from types import SimpleNamespace

from twitch_shorts.config import LLMConfig
from twitch_shorts.llm import rerank_with_claude
from twitch_shorts.models import Highlight


class FakeClient:
    def __init__(self, text, stop_reason="end_turn"):
        self.kwargs = None
        outer = self

        class Messages:
            def create(self, **kwargs):
                outer.kwargs = kwargs
                return SimpleNamespace(stop_reason=stop_reason,
                                       content=[SimpleNamespace(type="text", text=text)])

        self.beta = SimpleNamespace(messages=Messages())


def _cands():
    return [Highlight(start=0, end=30, peak=15, score=9.0, chat_sample=["草"]),
            Highlight(start=100, end=140, peak=120, score=4.0, chat_sample=["うおお"])]


def test_rerank_orders_by_llm_and_sets_titles():
    text = json.dumps({"candidates": [
        {"id": 0, "score": 2, "title": "雑談", "reason": "r0", "start": 0, "end": 30},
        {"id": 1, "score": 10, "title": "神プレイ", "hook": "まさかの結末", "reason": "r1", "start": 105, "end": 135},
    ]}, ensure_ascii=False)
    client = FakeClient(text)
    out = rerank_with_claude(_cands(), LLMConfig(), "yuuki_ftw", "配信", 15, 59, client=client)
    assert [h.title for h in out] == ["神プレイ", "雑談"]
    assert out[0].hook == "まさかの結末"
    assert "hook" in client.kwargs["output_config"]["format"]["schema"]["properties"]["candidates"]["items"]["required"]
    assert (out[0].start, out[0].end) == (105, 135)
    assert client.kwargs["model"] == "claude-opus-5-5"
    assert client.kwargs["output_config"]["format"]["type"] == "json_schema"
    assert client.kwargs["fallbacks"] == "default"


def test_rerank_rejects_out_of_range_boundaries():
    text = json.dumps({"candidates": [
        {"id": 0, "score": 5, "title": "a", "reason": "", "start": -50, "end": 300},
        {"id": 1, "score": 5, "title": "b", "reason": "", "start": 100, "end": 105},
    ]})
    out = rerank_with_claude(_cands(), LLMConfig(), "c", "", 15, 59, client=FakeClient(text))
    spans = sorted((h.start, h.end) for h in out)
    assert spans == [(0, 30), (100, 140)]


def test_refusal_keeps_signal_order():
    cands = _cands()
    out = rerank_with_claude(cands, LLMConfig(), "c", "", 15, 59, client=FakeClient("", "refusal"))
    assert out == cands and out[0].title == ""


def test_invalid_json_keeps_signal_order():
    cands = _cands()
    assert rerank_with_claude(cands, LLMConfig(), "c", "", 15, 59, client=FakeClient("not json")) == cands


def test_rerank_sets_category_description_and_hashtags():
    text = json.dumps({"candidates": [
        {"id": 0, "score": 8, "title": "神エイム", "hook": "これ見て", "category": "スーパープレイ",
         "description": "1v3 を制した瞬間。", "hashtags": ["#APEX", "クラッチ"], "reason": "", "start": 0, "end": 30},
    ]}, ensure_ascii=False)
    client = FakeClient(text)
    [h, _] = rerank_with_claude(_cands(), LLMConfig(), "yuuki_ftw", "", 15, 59, client=client)
    assert (h.category, h.description, h.hashtags) == ("スーパープレイ", "1v3 を制した瞬間。", ["#APEX", "#クラッチ"])
    item = client.kwargs["output_config"]["format"]["schema"]["properties"]["candidates"]["items"]
    assert item["properties"]["category"]["enum"][0] == "面白い"
