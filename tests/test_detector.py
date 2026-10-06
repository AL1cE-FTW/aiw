import random

import numpy as np

from twitch_shorts.config import DetectConfig
from twitch_shorts.detector import (
    build_signals, detect_highlights, keyword_weight, local_zscore, snap_to_transcript,
)
from twitch_shorts.models import ChatMessage, ClipRef, Highlight, TranscriptSegment


def _noise_chat(duration, rng):
    return [ChatMessage(t + rng.random(), f"u{rng.randint(0, 300)}", "こんにちは")
            for t in range(duration) for _ in range(rng.choice([0, 1, 1, 2]))]


def _burst(at, rng, n=15, length=15):
    return [ChatMessage(t + rng.random(), f"v{rng.randint(0, 10_000)}", rng.choice(["草", "KEKW", "wwww"]))
            for t in range(at, at + length) for _ in range(n)]


def test_detects_chat_bursts_and_corrects_delay():
    rng = random.Random(0)
    chat = _noise_chat(3600, rng)
    for ev in (600, 1800, 3000):
        chat += _burst(ev + 3, rng)
    cfg = DetectConfig()
    hs, score = detect_highlights(3600, chat, cfg)
    assert len(score) == 3600
    assert sorted(round(h.peak, -2) for h in hs) == [600, 1800, 3000]
    for h in hs:
        # 出来事 (ev) が区間に含まれ、長さが制約内
        ev = round(h.peak, -2)
        assert h.start <= ev <= h.end
        assert cfg.min_duration <= h.duration <= cfg.max_duration
        assert any("KEKW" in s or "草" in s for s in h.chat_sample)


def test_pure_noise_produces_no_highlights():
    for seed in range(5):
        hs, _ = detect_highlights(3600, _noise_chat(3600, random.Random(seed)), DetectConfig())
        assert hs == []


def test_highlights_respect_min_gap_and_top_n():
    rng = random.Random(2)
    chat = _noise_chat(1200, rng)
    for ev in (100, 120, 500, 900):
        chat += _burst(ev, rng)
    cfg = DetectConfig(top_n=2, min_gap=30)
    hs, _ = detect_highlights(1200, chat, cfg)
    assert len(hs) == 2
    a, b = sorted(hs, key=lambda h: h.start)
    assert b.start - a.end >= 0


def test_existing_clips_and_audio_boost_score():
    cfg = DetectConfig(min_score=0.5, min_relative_score=0)
    loud = np.full(600, -40.0)
    loud[300:310] = -5.0
    clips = [ClipRef(offset=290, duration=30, views=500)]
    hs, _ = detect_highlights(600, [], cfg, loudness=loud, clips=clips)
    assert hs and hs[0].start <= 300 <= hs[0].end
    assert set(hs[0].signals) == {"chat", "keywords", "audio", "clips"}


def test_window_is_clamped_to_media_bounds():
    rng = random.Random(3)
    chat = _noise_chat(200, rng) + _burst(5, rng) + _burst(190, rng, length=8)
    hs, _ = detect_highlights(200, chat, DetectConfig())
    assert hs
    for h in hs:
        assert 0 <= h.start < h.end <= 200


def test_keyword_weight_is_capped_and_case_insensitive():
    kws = DetectConfig().keywords
    assert keyword_weight("KEKW", kws) > 0
    assert keyword_weight("草 KEKW POG LUL ww うおお クリップ", kws) == 3.0
    assert keyword_weight("こんにちは", kws) == 0


def test_build_signals_counts_unique_users_per_second():
    cfg = DetectConfig(chat_delay=0)
    chat = [ChatMessage(10.1, "a", "x"), ChatMessage(10.5, "a", "y"), ChatMessage(10.7, "b", "z")]
    sig = build_signals(20, chat, cfg)
    assert sig["chat"][10] == 2


def test_local_zscore_handles_constant_series():
    assert np.all(local_zscore(np.ones(100)) == 0)


def test_snap_to_transcript_moves_edges_to_sentence_boundaries():
    cfg = DetectConfig()
    h = Highlight(start=100.0, end=130.0, peak=115, score=5)
    segs = [TranscriptSegment(97.5, 102.0, "a"), TranscriptSegment(103, 110, "b"),
            TranscriptSegment(128.0, 132.5, "c")]
    snap_to_transcript(h, segs, cfg)
    assert h.start == 97.5
    assert h.end == 132.5
