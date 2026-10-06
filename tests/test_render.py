from twitch_shorts.audio import loudness_per_second, probe_duration, probe_video_size
from twitch_shorts.config import RenderConfig
from twitch_shorts.models import TranscriptSegment
from twitch_shorts.render import build_ass, render_short, wrap_text


def test_wrap_text_counts_fullwidth_and_keeps_words():
    lines = wrap_text("神エイム炸裂！まさかの1v3クラッチで大逆転 Victory", 10)
    assert all(sum(1 if ord(c) > 0x2E80 else 0.55 for c in l) <= 10.6 for l in lines)
    assert any("Victory" in l for l in lines)


def test_wrap_text_truncates_extra_lines():
    lines = wrap_text("あ" * 100, 10, max_lines=2)
    assert len(lines) == 2 and lines[-1].endswith("…")


def test_build_ass_escapes_and_offsets_segments():
    ass = build_ass(RenderConfig(), 20, "タイトル{bad}", [
        TranscriptSegment(105, 108, "こんにちは"),
        TranscriptSegment(50, 60, "範囲外"),
    ], clip_start=100)
    assert "タイトル｛bad｝" in ass
    assert "0:00:05.00,0:00:08.00,Sub" in ass
    assert "範囲外" not in ass


def test_render_all_layouts(make_stream, tmp_path):
    video, _ = make_stream(duration=20, events=(5,))
    for layout in ("blur", "crop", "facecam"):
        cfg = RenderConfig(layout=layout, width=360, height=640, title_font_size=30, subtitle_font_size=24)
        out = render_short(str(video), str(tmp_path / f"{layout}.mp4"), 2, 12, cfg, title="テスト",
                           segments=[TranscriptSegment(3, 6, "字幕")])
        assert probe_video_size(out) == (360, 640)
        assert abs(probe_duration(out) - 10) < 0.3


def test_loudness_detects_loud_section(make_stream):
    video, _ = make_stream(duration=30, events=(10,))
    loud = loudness_per_second(str(video))
    assert 29 <= len(loud) <= 31
    assert loud[12] - loud[25] > 15  # 大きい区間とそれ以外で 15dB 以上の差
