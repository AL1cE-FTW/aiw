import json

from twitch_shorts.chat import load_chat
from twitch_shorts.config import Config
from twitch_shorts.audio import probe_video_size
from twitch_shorts.models import Highlight
from twitch_shorts.pipeline import LocalSource, process


def _cfg(tmp_path):
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    cfg.render.title_font_size, cfg.render.subtitle_font_size = 30, 24
    return cfg


def test_local_end_to_end(make_stream, tmp_path):
    video, chat_path = make_stream(duration=150, events=(40, 110))
    cfg = _cfg(tmp_path)
    run_dir = tmp_path / "out" / "run"
    result = process(cfg, LocalSource(str(video)), load_chat(chat_path), run_dir, channel="yuuki_ftw")
    assert len(result.highlights) == 2
    for h, ev in zip(result.highlights, (40, 110)):
        assert h.start <= ev + 4 <= h.end
        assert probe_video_size(h.output_path) == (360, 640)
        assert h.title.startswith("yuuki_ftw")
    report = json.loads((run_dir / "highlights.json").read_text(encoding="utf-8"))
    assert [round(h["peak"], -1) for h in report["highlights"]] == [40, 110]


def test_rolling_excludes_done_and_unfinished_tail(make_stream, tmp_path):
    video, chat_path = make_stream(duration=150, events=(40, 110))
    cfg = _cfg(tmp_path)
    chat = load_chat(chat_path)
    done = [Highlight(start=25, end=55, peak=40, score=9, output_path="x")]
    # 2 つ目の山 (110 秒付近) はまだ録画途中扱い
    r = process(cfg, LocalSource(str(video)), chat, tmp_path / "r", exclude=done, available_until=100, dry_run=True)
    assert r.highlights == []
    r = process(cfg, LocalSource(str(video)), chat, tmp_path / "r", exclude=done, dry_run=True)
    assert len(r.highlights) == 1 and r.highlights[0].start > 55


def test_transcribe_and_llm_integration(make_stream, tmp_path, monkeypatch):
    import twitch_shorts.transcribe as tr_mod
    from twitch_shorts.models import TranscriptSegment

    from .test_llm import FakeClient

    class FakeTranscriber:
        def __init__(self, cfg):
            pass

        def transcribe_range(self, media, start, end):
            return [TranscriptSegment(38.0, 44.0, "うわああ今の見た？"), TranscriptSegment(45, 50, "やばい")]

    monkeypatch.setattr(tr_mod, "Transcriber", FakeTranscriber)
    video, chat_path = make_stream(duration=120, events=(40,))
    cfg = _cfg(tmp_path)
    cfg.transcribe.enabled = True
    cfg.llm.enabled = True
    client = FakeClient(json.dumps({"candidates": [
        {"id": 0, "score": 9, "title": "まさかの展開", "reason": "驚き", "start": 26, "end": 48}]},
        ensure_ascii=False))
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), tmp_path / "t", channel="yuuki_ftw",
                llm_client=client)
    [h] = r.highlights
    assert h.title == "まさかの展開" and h.reason == "驚き"
    assert (h.start, h.end) == (26, 48)
    assert "今の見た" in h.transcript
    sent = json.loads(client.kwargs["messages"][0]["content"])
    assert sent["channel"] == "yuuki_ftw" and "今の見た" in sent["candidates"][0]["transcript"]
    assert probe_video_size(h.output_path) == (360, 640)


def test_vod_source_renders_from_section_with_offset(make_stream, tmp_path, monkeypatch):
    import subprocess

    import twitch_shorts.download as dl
    from twitch_shorts.audio import probe_duration
    from twitch_shorts.pipeline import VodSource

    video, chat_path = make_stream(duration=120, events=(70,))
    sections = []

    def fake_audio(url, out_dir):
        return video

    def fake_section(url, start, end, out, quality):
        sections.append((start, end))
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-i", str(video), "-t", str(end - start),
                        "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(out)], check=True)
        return out

    monkeypatch.setattr(dl, "download_audio", fake_audio)
    monkeypatch.setattr(dl, "download_section", fake_section)
    cfg = _cfg(tmp_path)
    src = VodSource("https://www.twitch.tv/videos/1", tmp_path / "work")
    r = process(cfg, src, load_chat(chat_path), tmp_path / "v", duration=120)
    [h] = r.highlights
    [(a, b)] = sections
    assert a <= h.start and h.end <= b
    assert abs(probe_duration(h.output_path) - h.duration) < 0.3
