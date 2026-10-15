import json
from types import SimpleNamespace

from twitch_shorts.config import LLMConfig
from twitch_shorts.llm import rerank_with_claude, rerank_with_llm
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


# --- Gemini -----------------------------------------------------------------

class FakeGemini:
    def __init__(self, text, finish="STOP"):
        self.kwargs = None
        outer = self

        class Models:
            def generate_content(self, **kwargs):
                outer.kwargs = kwargs
                cand = SimpleNamespace(finish_reason=SimpleNamespace(name=finish))
                return SimpleNamespace(candidates=[cand], text=text)

        self.models = Models()


def test_gemini_provider_uses_same_prompt_and_schema():
    text = json.dumps({"candidates": [
        {"id": 0, "score": 1, "title": "雑談", "reason": "", "start": 0, "end": 30},
        {"id": 1, "score": 9, "title": "神プレイ", "hook": "これ見て", "reason": "", "start": 100, "end": 140},
    ]}, ensure_ascii=False)
    client = FakeGemini(text)
    cfg = LLMConfig(provider="gemini")  # model は claude の既定のまま → Gemini の既定に切り替わる
    out = rerank_with_llm(_cands(), cfg, "yuuki_ftw", "配信", 15, 59, client=client)
    assert [h.title for h in out] == ["神プレイ", "雑談"] and out[0].hook == "これ見て"
    assert client.kwargs["model"] == "gemini-flash-latest"
    conf = client.kwargs["config"]
    assert conf["response_mime_type"] == "application/json"
    assert "hook" in conf["response_json_schema"]["properties"]["candidates"]["items"]["required"]
    assert "切り抜き編集者" in conf["system_instruction"]


def test_gemini_truncated_or_blocked_keeps_signal_order():
    cands = _cands()
    cfg = LLMConfig(provider="gemini")
    assert rerank_with_llm(cands, cfg, "c", "", 15, 59, client=FakeGemini('{"cand', "MAX_TOKENS")) == cands
    assert rerank_with_llm(cands, cfg, "c", "", 15, 59, client=FakeGemini(None, "SAFETY")) == cands


def test_model_follows_provider():
    from twitch_shorts.llm import model_of

    assert model_of(LLMConfig(provider="gemini", model="gemini-2.5-flash")) == "gemini-2.5-flash"
    assert model_of(LLMConfig(provider="claude", model="gemini-flash-latest")) == "claude-opus-5-5"
    assert model_of(LLMConfig(provider="claude")) == "claude-opus-5-5"


def test_unknown_provider_is_reported():
    import pytest

    with pytest.raises(ValueError):
        rerank_with_llm(_cands(), LLMConfig(provider="chatgpt"), "c", "", 15, 59, client=object())


def test_misspelled_provider_stops_at_config_load(tmp_path):
    import pytest

    from twitch_shorts.config import load_config

    p = tmp_path / "config.toml"
    p.write_text('[llm]\nenabled = true\nprovider = "Gemni"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(p))
    p.write_text('[llm]\nenabled = true\nprovider = 1\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(p))
    p.write_text('[llm]\nenabled = false\nprovider = "Gemni"\n', encoding="utf-8")
    load_config(str(p))  # AI を使わない設定なら止めない
    p.write_text('[llm]\nprovider = " Gemini "\n', encoding="utf-8")
    assert load_config(str(p)).llm.provider == "gemini"


def test_llm_flag_with_misspelled_provider_exits(tmp_path, monkeypatch):
    from twitch_shorts import cli

    p = tmp_path / "config.toml"
    p.write_text('[llm]\nprovider = "Gemni"\n', encoding="utf-8")
    monkeypatch.setattr(cli, "cmd_local", lambda cfg, args: 0, raising=False)
    assert cli.main(["-c", str(p), "local", str(tmp_path / "x.mp4"), "--llm"]) == 2
    assert cli.main(["-c", str(tmp_path / "missing.toml"), "doctor"]) == 2


def test_falsy_provider_is_rejected():
    import pytest

    from twitch_shorts.config import normalize_provider

    with pytest.raises(ValueError):
        normalize_provider(False)
    assert normalize_provider(None) == "claude"


# --- Gemini が音声を聞いて場面を選ぶ ---------------------------------------------

class FakeGeminiFiles:
    def __init__(self, text):
        self.deleted = None
        self.contents = None
        outer = self

        class Files:
            def upload(self, file):
                return SimpleNamespace(name="files/1", state=SimpleNamespace(name="PROCESSING"))

            def get(self, name):
                return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

            def delete(self, name):
                outer.deleted = name

        class Models:
            def generate_content(self, **kwargs):
                outer.contents = kwargs["contents"]
                return SimpleNamespace(text=text)

        self.files = Files()
        self.models = Models()


def test_gemini_scan_audio_picks_moments(monkeypatch):
    from twitch_shorts import llm

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    text = json.dumps({"moments": [
        {"start": "0:10:00", "end": "0:10:30", "score": 9, "title": "t", "hook": "h", "category": "面白い",
         "description": "d", "hashtags": ["apex", "#yuuki"], "reason": "爆笑"},
        {"start": "10:20", "end": "10:40", "score": 5, "title": "重なり", "hook": "", "category": "その他",
         "description": "", "hashtags": [], "reason": ""},  # 1 件目と重なる → 捨てる
        {"start": "1:59:55", "end": "2:05:00", "score": 7, "title": "最後", "hook": "", "category": "その他",
         "description": "", "hashtags": [], "reason": ""},  # 終わりを超える → 配信の長さに収める
        {"start": "x", "end": "y", "score": 1},  # 読めない → 捨てる
        {"start": "2:30:00", "end": "2:30:30", "score": 10, "title": "配信の外", "hook": "", "category": "その他",
         "description": "", "hashtags": [], "reason": ""},  # 配信より後 → 端に寄せずに捨てる
    ]})
    client = FakeGeminiFiles(text)
    found = llm.scan_audio_with_gemini("a.mp4", LLMConfig(provider="gemini"), "c", "s", 7200, 15, 59, 9,
                                       client=client)
    assert [(h.start, h.end, h.title) for h in found] == [(600, 630, "t"), (7185, 7200, "最後")]
    assert found[0].signals == {"ai": 9} and found[0].hashtags == ["#apex", "#yuuki"]
    assert client.deleted == "files/1" and client.contents[0].state.name == "ACTIVE"


def test_gemini_rerank_watches_candidate_videos(monkeypatch):
    from twitch_shorts import llm

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    cands = [Highlight(start=10, end=40, peak=20, score=5.0), Highlight(start=100, end=130, peak=110, score=4.0)]
    text = json.dumps({"candidates": [
        {"id": 0, "score": 2, "title": "a", "hook": "", "category": "その他", "description": "", "hashtags": [],
         "reason": "", "start": 10, "end": 40},
        {"id": 1, "score": 9, "title": "b", "hook": "", "category": "面白い", "description": "", "hashtags": [],
         "reason": "", "start": 100, "end": 130},
    ]})
    client = FakeGeminiFiles(text)
    out = rerank_with_llm(cands, LLMConfig(provider="gemini"), "c", "", 15, 59, client=client,
                          videos={1: "v1.mp4"})
    assert [h.title for h in out] == ["b", "a"]
    # 文字情報 → 候補 1 の説明 → その映像 の順に送り、送った映像は消す
    assert client.contents[1] == "候補 id=1 の映像 (映像の 0 秒 = この候補の start)"
    assert client.contents[2].state.name == "ACTIVE" and client.deleted == "files/1"


def test_gemini_rerank_uploads_all_videos_before_waiting(monkeypatch):
    from twitch_shorts import llm

    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    events = []

    class Files:
        def upload(self, file):
            events.append(("upload", file))
            return SimpleNamespace(name=file, state=SimpleNamespace(name="PROCESSING"))

        def get(self, name):
            events.append(("get", name))
            return SimpleNamespace(name=name, state=SimpleNamespace(name="ACTIVE"))

        def delete(self, name):
            events.append(("delete", name))

    class Models:
        def generate_content(self, **kwargs):
            return SimpleNamespace(text=json.dumps({"candidates": []}))

    client = SimpleNamespace(files=Files(), models=Models())
    cands = [Highlight(start=10, end=40, peak=20, score=5.0), Highlight(start=100, end=130, peak=110, score=4.0)]
    rerank_with_llm(cands, LLMConfig(provider="gemini"), "c", "", 15, 59, client=client,
                    videos={0: "v0.mp4", 1: "v1.mp4"})
    # 1 本ずつ準備を待たずに、先に全部送ってから待つ
    assert events[:4] == [("upload", "v0.mp4"), ("upload", "v1.mp4"), ("get", "v0.mp4"), ("get", "v1.mp4")]
    assert {e for e in events if e[0] == "delete"} == {("delete", "v0.mp4"), ("delete", "v1.mp4")}


def test_require_stops_instead_of_signal_only():
    import pytest

    cands = [Highlight(start=0, end=30, peak=10, score=1.0)]
    with pytest.raises(RuntimeError):
        rerank_with_llm(cands, LLMConfig(provider="gemini", require=True), "c", "", 15, 59,
                        client=FakeGemini(None, "SAFETY"))
