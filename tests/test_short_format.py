"""ショートの型 (先見せ・フック文・固定位置テロップ・投稿予定表) のテスト。"""

import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from twitch_shorts.audio import probe_duration, probe_video_size
from twitch_shorts.config import Config, RenderConfig
from twitch_shorts.models import Highlight, TranscriptSegment
from twitch_shorts.render import build_ass, hook_range, render_highlight, split_captions
from twitch_shorts.schedule import add_to_schedule, load_schedule


def test_split_captions_respects_max_chars_and_punctuation():
    caps = split_captions("いやいやちょっと待って、今の見た？やばすぎるでしょ", 0, 9, 10)
    texts = [c for _, _, c in caps]
    assert all(len(t) <= 10 for t in texts)
    assert "".join(texts) == "いやいやちょっと待って、今の見た？やばすぎるでしょ"
    assert texts[-1] == "やばすぎるでしょ"
    # 表示時間は連続していて全体を埋める
    assert caps[0][0] == 0 and abs(caps[-1][1] - 9) < 1e-6
    assert all(abs(a[1] - b[0]) < 1e-6 for a, b in zip(caps, caps[1:]))
    assert [c for _, _, c in split_captions("Nice shot! これはうまい", 0, 1, 10)] == ["Nice shot!", "これはうまい"]


def test_captions_use_one_fixed_position_and_skip_hook_time():
    cfg = RenderConfig()
    ass = build_ass(cfg, 30, "タイトル", [TranscriptSegment(101, 104, "最初の発話"),
                                         TranscriptSegment(110, 115, "これは長めの発話で、テロップが分かれる")],
                    clip_start=100, hook_text="まさかの1v3", lead_in=2.0)
    positions = set(re.findall(r"\\pos\((\d+),(\d+)\)", ass))
    assert positions == {(str(cfg.width // 2), str(int(cfg.height * cfg.caption_position)))}
    hook_line = next(l for l in ass.splitlines() if ",Hook," in l)
    assert "0:00:00.00,0:00:03.00" in hook_line
    subs = [l for l in ass.splitlines() if ",Sub," in l]
    # 1 つ目の発話 (先見せ後 3〜6 秒) はフック文の後から表示される
    assert subs[0].split(",")[1] == "0:00:03.00"
    assert all(len(l.rsplit("}", 1)[1]) <= cfg.caption_max_chars for l in subs)


def test_hook_range_stays_inside_highlight():
    cfg = RenderConfig(hook_seconds=2.0)
    assert hook_range(Highlight(start=10, end=40, peak=30, score=1), cfg) == (29.5, 31.5)
    assert hook_range(Highlight(start=10, end=40, peak=39.8, score=1), cfg) == (39.3, 40)
    assert hook_range(Highlight(start=10, end=40, peak=30, score=1), RenderConfig(hook_seconds=0)) is None


def test_render_with_hook_keeps_total_under_limit(make_stream, tmp_path):
    video, _ = make_stream(duration=80, events=(40,))
    cfg = RenderConfig(width=360, height=640, title_font_size=30, subtitle_font_size=30, hook_font_size=36)
    h = Highlight(start=10, end=69, peak=42, score=5, title="テスト", hook="これ見て")
    out = render_highlight(str(video), str(tmp_path / "h.mp4"), h, cfg, max_total=59)
    assert probe_video_size(out) == (360, 640)
    assert abs(probe_duration(out) - 59) < 0.3  # 59 秒本編 + 2 秒先見せ → 前フリを削って 59 秒


def _cfg(tmp_path):
    return Config(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"))


def test_schedule_assigns_one_daily_slot_best_first(tmp_path):
    cfg = _cfg(tmp_path)
    tz = ZoneInfo("Asia/Tokyo")
    now = datetime(2026, 10, 6, 20, 0, tzinfo=tz)  # 今日の 19:00 枠は過ぎている
    hs = [Highlight(start=0, end=30, peak=10, score=s, title=f"s{s}", output_path=f"/x/{s}.mp4") for s in (3, 9, 5)]
    added = add_to_schedule(cfg, hs, "yuuki_ftw", now=now)
    assert [e["title"] for e in added] == ["s9", "s5", "s3"]
    assert [e["publish_at"][:16] for e in added] == ["2026-10-07T19:00", "2026-10-08T19:00", "2026-10-09T19:00"]
    # 次の実行分は空いている次の日から。同じファイルは二重登録しない
    more = [Highlight(start=0, end=30, peak=10, score=7, title="next", output_path="/x/next.mp4"), hs[0]]
    added2 = add_to_schedule(cfg, more, "yuuki_ftw", now=now)
    assert [(e["title"], e["publish_at"][:10]) for e in added2] == [("next", "2026-10-10")]
    assert len(load_schedule(cfg)) == 4
    csv_text = (tmp_path / "out" / "schedule.csv").read_text(encoding="utf-8-sig")
    assert csv_text.splitlines()[0].startswith("publish_at,title,hook")


def test_schedule_today_slot_if_not_passed_and_multiple_per_day(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.publish.posts_per_day = 2
    tz = ZoneInfo("Asia/Tokyo")
    now = datetime(2026, 10, 6, 9, 0, tzinfo=tz)
    hs = [Highlight(start=0, end=30, peak=10, score=s, title=str(s), output_path=f"/x/{s}.mp4") for s in (1, 2, 3)]
    added = add_to_schedule(cfg, hs, now=now)
    assert [e["publish_at"][:16] for e in added] == ["2026-10-06T19:00", "2026-10-07T07:00", "2026-10-07T19:00"]
    data = json.loads((tmp_path / "out" / "schedule.json").read_text(encoding="utf-8"))
    assert [d["title"] for d in data] == ["3", "2", "1"]


# --- レビュー指摘の回帰テスト -------------------------------------------------

def test_trim_never_cuts_the_peak(make_stream, tmp_path):
    video, _ = make_stream(duration=80, events=(10,))
    cfg = RenderConfig(width=360, height=640, title_font_size=30, subtitle_font_size=30, hook_font_size=36)
    h = Highlight(start=10, end=69, peak=11, score=5)
    render_highlight(str(video), str(tmp_path / "p.mp4"), h, cfg, max_total=59)
    assert h.start <= h.peak - 1 and h.end - h.start == 57  # 末尾側を削って 57 秒 + 先見せ 2 秒
    assert h.video_duration == 59
    assert abs(probe_duration(str(tmp_path / "p.mp4")) - 59) < 0.3


def test_captions_overlapping_hook_keep_original_timing():
    cfg = RenderConfig()
    seg = TranscriptSegment(100.5, 106, "最初の言葉、次の言葉、最後の言葉")
    ass = build_ass(cfg, 20, "", [seg], clip_start=100, hook_text="これ見て", lead_in=2.0)
    subs = [l for l in ass.splitlines() if ",Sub," in l]
    # 本来 2.5〜8 秒に文字量で割り振られる 2 枚 (「最初の言葉、」「次の言葉、最後の言葉」) のうち、
    # フック文 (〜3 秒) に重なる 1 枚目の頭だけが削られ、2 枚目は本来のタイミングのまま
    assert [l.split(",")[1:3] for l in subs] == [["0:00:03.00", "0:00:04.56"], ["0:00:04.56", "0:00:08.00"]]
    assert subs[-1].endswith("次の言葉、最後の言葉")


def test_long_words_are_split_and_spaces_kept():
    caps = [c for _, _, c in split_captions("supercalifragilisticexpialidocious wow", 0, 5, 10)]
    assert all(sum(1 if ord(ch) > 0x2E80 else 0.55 for ch in c) <= 10 for c in caps)
    assert caps[-1].endswith(" wow") or caps[-1] == "wow"
    assert "".join(caps).replace(" ", "") == "supercalifragilisticexpialidociouswow"


def test_schedule_uses_previous_days_early_slot(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.publish.posts_per_day = 2
    now = datetime(2026, 10, 7, 5, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    [e] = add_to_schedule(cfg, [Highlight(start=0, end=30, peak=5, score=1, output_path="/x/a.mp4")], now=now)
    assert e["publish_at"][:16] == "2026-10-07T07:00"


def test_bad_publish_config_does_not_lose_results(make_stream, tmp_path, caplog):
    from twitch_shorts.chat import load_chat
    from twitch_shorts.pipeline import LocalSource, process

    video, chat_path = make_stream(duration=100, events=(50,))
    cfg = _cfg(tmp_path)
    cfg.render.width, cfg.render.height = 360, 640
    cfg.publish.timezone = "JST"  # 不正なタイムゾーン
    r = process(cfg, LocalSource(str(video)), load_chat(chat_path), tmp_path / "out" / "run")
    assert r.highlights and (tmp_path / "out" / "run" / "highlights.json").exists()
    assert "投稿予定表に追加できませんでした" in caplog.text
