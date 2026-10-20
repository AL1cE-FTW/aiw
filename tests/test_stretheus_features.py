"""無音カット・コメント字幕・SRT・ストリームマーカー・区間の指定 (Stretheus の機能を参考にしたもの) のテスト。"""

import json
import subprocess

import pytest

from twitch_shorts.audio import probe_duration, silent_ranges
from twitch_shorts.chat import load_chat
from twitch_shorts.config import Config, RenderConfig, load_config
from twitch_shorts.models import ChatMessage, Highlight, TranscriptSegment
from twitch_shorts.pipeline import LocalSource, process
from twitch_shorts.render import TimeMap, ass_color, build_ass, build_srt, keep_ranges, render_highlight
from twitch_shorts.twitch_auth import save_token
from twitch_shorts.twitch_markers import Marker, get_markers, markers_for_vod

from .conftest import FakeResponse, FakeSession


def _cfg(tmp_path):
    cfg = Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))
    cfg.render.width, cfg.render.height = 360, 640
    cfg.render.title_font_size, cfg.render.subtitle_font_size, cfg.render.comment_font_size = 30, 24, 20
    return cfg


# --- 無音カット ---------------------------------------------------------------

def test_keep_ranges_and_time_map():
    keep = keep_ranges([(2.0, 5.0), (8.0, 8.5), (9.0, 10.0)], 10.0, pad=0.2)
    # 0.5 秒の無音は余白を残すと詰める所が無い。最後の無音は区間の終わりまで詰める
    assert keep == [(0.0, 2.2), (4.8, 9.2)]
    tmap = TimeMap(keep)
    assert tmap(1.0) == 1.0
    assert tmap(3.0) == pytest.approx(2.2)  # 詰めた間 → 次に残す所の頭
    assert tmap(5.0) == pytest.approx(2.4)
    assert tmap.removed(3.0) and not tmap.removed(6.0)
    assert keep_ranges([], 10.0, 0.2) == [(0.0, 10.0)]


def _tone_with_gap(path, duration=20, gap=(6, 11)):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=15:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={duration}",
         "-filter_complex", f"[1:a]volume='if(between(t,{gap[0]},{gap[1]}),0,1)':eval=frame[a]",
         "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(path)],
        check=True)
    return path


def test_silence_cut_shortens_video_and_shifts_captions(tmp_path):
    video = _tone_with_gap(tmp_path / "v.mp4")
    gaps = silent_ranges(str(video), 2, 16)
    assert len(gaps) == 1 and gaps[0][0] == pytest.approx(4, abs=0.3) and gaps[0][1] == pytest.approx(9, abs=0.3)

    cfg = RenderConfig(width=360, height=640, title_font_size=30, subtitle_font_size=24, silence_cut=True,
                       export_srt=True, hook_seconds=0, fade=0)
    h = Highlight(start=2, end=18, peak=10, score=5, title="t")
    out = tmp_path / "s.mp4"
    render_highlight(str(video), str(out), h, cfg, [TranscriptSegment(14, 16, "あとの発話")])
    # 16 秒の区間から約 5 秒 (余白 0.3 秒を残す) の無音を詰める
    assert h.video_duration == pytest.approx(11.3, abs=0.4)
    assert probe_duration(str(out)) == pytest.approx(h.video_duration, abs=0.3)
    srt = (tmp_path / "s.srt").read_text(encoding="utf-8")
    # 14 秒の発話は本編の 12 秒目 → 詰めた分だけ前 (約 7.3 秒目) に出る
    assert "00:00:07," in srt and "あとの発話" in srt


# --- コメント字幕 ---------------------------------------------------------------

def test_comments_are_drawn_with_icon_and_box():
    cfg = RenderConfig(comments=True, comment_lines=2, comment_seconds=3.0)
    chat = [ChatMessage(101, "alice", "草"), ChatMessage(101.5, "bob", "近すぎる"),  # 間隔が短いので出さない
            ChatMessage(103, "carol", "!command"), ChatMessage(104, "dave", "見て http://x"),
            ChatMessage(105, "erin", "うおおおおおおおおおおおおおおおおおおおおお"), ChatMessage(130, "late", "範囲外")]
    ass = build_ass(cfg, 20, "", [], clip_start=100, comments=chat)
    texts = [l for l in ass.splitlines() if l.startswith("Dialogue: 5") and "\\an4" in l]
    assert [t.rsplit("}", 1)[1] for t in texts] == ["草", "うおおおおおおおおおおおおおお…"]
    assert "\\p1" in ass and "\\1a&H60&" in ass  # 半透明の角丸の箱とアイコンを描く
    assert "}A" in ass and "}E" in ass  # 名前の頭文字


def test_comments_off_by_default(make_stream, tmp_path):
    video, chat_path = make_stream(duration=30, events=(10,))
    chat = load_chat(chat_path)
    for comments in (False, True):
        cfg = RenderConfig(width=360, height=640, title_font_size=30, subtitle_font_size=24, comment_font_size=20,
                           comments=comments)
        h = Highlight(start=5, end=25, peak=12, score=5, title="t")
        render_highlight(str(video), str(tmp_path / f"{comments}.mp4"), h, cfg, chat=chat)
        assert probe_duration(str(tmp_path / f"{comments}.mp4")) == pytest.approx(22, abs=0.3)


# --- SRT・色 ---------------------------------------------------------------------

def test_srt_has_hook_text_and_captions():
    srt = build_srt(RenderConfig(), 30, [TranscriptSegment(110, 112, "こんにちは")], 100, hook_text="まさかの",
                    lead_in=2.0)
    assert srt.startswith("1\n00:00:00,000 --> 00:00:03,000\nまさかの\n\n2\n00:00:12,000 --> 00:00:14,000\nこんにちは")


def test_subtitle_colors(tmp_path):
    assert ass_color("#FF8800") == "&H000088FF"
    ass = build_ass(RenderConfig(subtitle_color="#FFFF00", subtitle_outline_color="#112233"), 10, "", [], 0)
    assert "Style: Sub," in ass and "&H0000FFFF,&H00FFFFFF,&H00332211" in ass
    bad = tmp_path / "c.toml"
    bad.write_text('[render]\nsubtitle_color = "yellow"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="subtitle_color"):
        load_config(bad)


# --- ストリームマーカー -----------------------------------------------------------

class Resp(FakeResponse):
    def __init__(self, payload, status=200):
        super().__init__(payload, status, {"content-type": "application/json"})


def test_get_markers_pages_through_results(tmp_path):
    save_token(tmp_path, {"access_token": "at", "refresh_token": "rt", "expires_at": 9e12, "user_id": "42",
                          "login": "yuuki_ftw", "scopes": ["user:read:broadcast"]})
    from twitch_shorts.twitch_auth import UserToken

    def handler(method, url, params, data):
        assert url.endswith("/streams/markers") and params["video_id"] == "123"
        if "after" not in params:
            return Resp({"data": [{"videos": [{"video_id": "123", "markers": [
                {"id": "a", "position_seconds": 600, "description": "神プレイ"}]}]}], "pagination": {"cursor": "c"}})
        return Resp({"data": [{"videos": [{"video_id": "123", "markers": [
            {"id": "b", "position_seconds": 120, "description": ""}]}]}], "pagination": {}})

    session = FakeSession(handler)
    markers = get_markers("cid", UserToken("cid", "", tmp_path), "123", session=session)
    assert markers == [Marker(120, ""), Marker(600, "神プレイ")]
    assert len(session.calls) == 2


def test_markers_need_the_new_scope(tmp_path, caplog):
    assert markers_for_vod("cid", "", str(tmp_path), "123") == []  # 未ログイン
    save_token(tmp_path, {"access_token": "at", "scopes": ["channel:manage:clips"]})
    with caplog.at_level("INFO"):
        assert markers_for_vod("cid", "", str(tmp_path), "123") == []
    assert "login" in caplog.text


def test_marker_scene_is_always_made_first(make_stream, tmp_path):
    video, chat_path = make_stream(duration=150, events=(40,))
    cfg = _cfg(tmp_path)
    cfg.detect.top_n = 1
    # チャットの盛り上がりは 40 秒付近だが、配信者がマーカーを付けた 120 秒の場面を優先する (VOD は 5 秒ずれている)
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), tmp_path / "m", dry_run=True,
                clip_vod=("123", 5.0), markers=[Marker(125, "ここ見て")])
    [h] = r.highlights
    assert (h.start, h.end) == (80, 130)
    assert h.signals == {"marker": 1.0} and h.reason == "ストリームマーカー: ここ見て"


def test_markers_outside_the_recording_are_ignored(make_stream, tmp_path):
    video, chat_path = make_stream(duration=150, events=(40,))
    cfg = _cfg(tmp_path)
    cfg.detect.top_n = 1
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), tmp_path / "m", dry_run=True,
                clip_vod=("123", 0.0), markers=[Marker(900, "")])
    [h] = r.highlights
    assert "marker" not in h.signals and h.start < 60


# --- 区間を指定して作る -------------------------------------------------------------

def test_ranges_make_exactly_those_scenes(make_stream, tmp_path):
    video, chat_path = make_stream(duration=150, events=(40,))
    cfg = _cfg(tmp_path)
    cfg.render.hook_seconds = 0
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), tmp_path / "r",
                ranges=[(100, 112.5), (10, 20)])
    assert [(h.start, h.end) for h in r.highlights] == [(10, 20), (100, 112.5)]
    assert all(probe_duration(h.output_path) == pytest.approx(h.end - h.start, abs=0.3) for h in r.highlights)
    with pytest.raises(ValueError):
        process(cfg, LocalSource(str(video)), [], tmp_path / "bad", ranges=[(100, 900)], dry_run=True)


def test_range_option(monkeypatch):
    from twitch_shorts import cli

    assert cli.parse_range("1:23:00-1:23:40.5") == (4980, 5020.5)
    assert cli.parse_range("90-120") == (90, 120)
    for bad in ("1:00", "2:00-1:00", "a-b", "1:-2:00"):
        with pytest.raises(Exception):
            cli.parse_range(bad)
    seen = {}
    monkeypatch.setattr("twitch_shorts.pipeline.process", lambda *a, **k: seen.update(k) or
                        type("R", (), {"highlights": []})())
    cli.main(["local", "x.mp4", "--range", "10-20", "--range", "1:00-1:30", "--silence-cut", "--comments"])
    assert seen["ranges"] == [(10, 20), (60, 90)]
