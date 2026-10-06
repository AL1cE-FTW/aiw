import json
import random

from twitch_shorts.config import DetectConfig
from twitch_shorts.detector import build_signals, detect_highlights, viewer_growth
from twitch_shorts.models import ChatMessage, ViewerSample
from twitch_shorts.viewers import ViewerRecorder, load_viewers


def _wobbly(n=3600, base=20, jump_at=None, jump=15, seed=0):
    rng = random.Random(seed)
    return [ViewerSample(t, base + rng.randint(-2, 2) + (jump if jump_at is not None and t >= jump_at else 0))
            for t in range(0, n, 60)]


def test_small_wobble_is_ignored_and_jump_is_detected():
    cfg = DetectConfig()
    for seed in range(5):
        assert abs(viewer_growth(_wobbly(seed=seed), 3600, cfg)).max() / 0.05 <= 1.0
    g = viewer_growth(_wobbly(jump_at=1830), 3600, cfg)
    # API 反映の遅れ (viewer_delay) を補正した位置で最大になる
    assert abs(int(g.argmax()) - (1830 - cfg.viewer_delay)) <= 60


def test_viewer_signal_only_when_samples_exist():
    cfg = DetectConfig()
    assert "viewers" not in build_signals(600, [], cfg)
    assert "viewers" not in build_signals(600, [], cfg, viewers=[ViewerSample(0, 5)])
    assert "viewers" in build_signals(600, [], cfg, viewers=_wobbly(600))


def test_viewer_jump_adds_to_a_chat_moment():
    rng = random.Random(1)
    chat = [ChatMessage(t + rng.random(), f"u{rng.randint(0, 50)}", "x") for t in range(3600) if rng.random() < 0.5]
    # 2 つの同程度の盛り上がり。視聴者が増えた方が上位になる
    for ev in (1000, 2500):
        chat += [ChatMessage(ev + t + rng.random(), f"v{rng.randint(0, 999)}", "x") for t in range(10) for _ in range(4)]
    cfg = DetectConfig(min_score=0, min_relative_score=0, top_n=2)
    hs, _ = detect_highlights(3600, chat, cfg, viewers=_wobbly(jump_at=2500 + 40))
    assert hs[0].start <= 2500 <= hs[0].end
    assert hs[0].signals["viewers"] > hs[1].signals["viewers"]


def test_load_viewers_formats(tmp_path):
    j = tmp_path / "v.jsonl"
    j.write_text('{"offset": 60, "viewers": 12}\n{"offset": 0, "viewers": 10}\n')
    assert [v.viewers for v in load_viewers(j)] == [10, 12]
    c = tmp_path / "v.csv"
    c.write_text("offset,viewers\n0,10\n60,11.0\n")
    assert [v.viewers for v in load_viewers(c)] == [10, 11]
    a = tmp_path / "v.json"
    a.write_text(json.dumps([{"offset": 5, "viewers": 3}]))
    assert load_viewers(a)[0].offset == 5


def test_viewer_recorder_appends_samples(tmp_path):
    class FakeHelix:
        def __init__(self):
            self.values = iter([{"viewer_count": 15}, None])

        def get_stream(self, login):
            assert login == "yuuki_ftw"
            return next(self.values)

    out = tmp_path / "viewers.jsonl"
    rec = ViewerRecorder(FakeHelix(), "yuuki_ftw", out, start_time=0, interval=60)
    assert rec.sample_once().viewers == 15
    assert rec.sample_once() is None  # オフラインなら記録しない
    [s] = load_viewers(out)
    assert s.viewers == 15 and rec.count == 1
