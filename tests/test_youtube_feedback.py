import json
import tomllib

from twitch_shorts import cli
from twitch_shorts.config import Config, load_config
from twitch_shorts.models import Highlight
from twitch_shorts.schedule import add_to_schedule
from twitch_shorts.youtube_feedback import (
    analyze_feedback, load_youtube_csv, map_columns, match_shorts, our_shorts, report_markdown, write_outputs,
)

EN = """Content,Video title,Video publish time,Duration,Views,Impressions,Impressions click-through rate (%),Average view duration,Average percentage viewed (%),Stayed to watch (%)
Total,,,,5000,90000,3.1,0:20,70.0,65.0
vid1,神エイム炸裂 #shorts #APEX,"Oct 7, 2026",35,3000,40000,4.0,0:30,85.5,80.0
vid2,【切り抜き】まさかの結末,"Oct 8, 2026",50,800,20000,2.0,0:20,40.0,55.0
vid3,長尺の配信アーカイブ,"Oct 8, 2026",7200,100,1000,1.0,10:00,10.0,
"""

JA = """﻿コンテンツ,動画のタイトル,動画公開時刻,長さ,視聴回数,インプレッション数,インプレッションのクリック率 (%),平均視聴時間,平均視聴率 (%),視聴を継続 (%)
合計,,,,10,100,1.0,0:10,50.0,60.0
abc,テスト動画,2026/10/07,30,"1,234",5000,2.5,0:25,83.3,72.5
"""


def test_map_columns_handles_english_and_japanese_and_avoids_lookalikes():
    en = map_columns(EN.splitlines()[0].split(","))
    assert en["impressions"] == "Impressions"
    assert en["duration"] == "Duration"  # "Average view duration" ではない
    assert en["stayed_pct"] == "Stayed to watch (%)"
    assert en["avg_viewed_pct"] == "Average percentage viewed (%)"
    ja = map_columns(JA.lstrip("﻿").splitlines()[0].split(","))
    assert ja["impressions"] == "インプレッション数"
    assert ja["stayed_pct"] == "視聴を継続 (%)" and ja["avg_viewed_pct"] == "平均視聴率 (%)"
    assert ja["duration"] == "長さ"


def test_load_csv_skips_total_and_long_videos(tmp_path):
    p = tmp_path / "Table data.csv"
    p.write_text(EN, encoding="utf-8")
    rows, missing = load_youtube_csv(p)
    assert [r.video_id for r in rows] == ["vid1", "vid2"]
    assert rows[0].stayed_pct == 80.0 and rows[0].avg_viewed_pct == 85.5 and rows[0].impressions == 40000
    assert missing == []
    j = tmp_path / "ja.csv"
    j.write_bytes(JA.encode("utf-8"))
    [r] = load_youtube_csv(j)[0]
    assert (r.title, r.views, r.stayed_pct) == ("テスト動画", 1234, 72.5)


def test_manual_csv_and_missing_columns(tmp_path):
    p = tmp_path / "m.csv"
    p.write_text("title,impressions\n神回,100\n", encoding="utf-8")
    rows, missing = load_youtube_csv(p)
    assert rows[0].impressions == 100 and "stayed_pct" in missing
    fb = analyze_feedback(Config(), rows, missing)
    assert any("視聴を継続" in n for n in fb.notes)


def _make_ours(tmp_path, n=6):
    cfg = Config(output_dir=str(tmp_path / "out"))
    hs = []
    for i in range(n):
        good = i < n // 2
        hs.append(Highlight(start=0, end=25 if good else 50, peak=10, score=5, title=f"切り抜き{i}号 すごい場面",
                            hook="これ見て" if good else "", output_path=f"/x/{i}.mp4",
                            signals={"chat": 5.0 if good else 1.0, "audio": 1.0 if good else 4.0, "keywords": 2.0}))
    add_to_schedule(cfg, hs, "yuuki_ftw")
    return cfg


def _yt_csv(tmp_path, n=6):
    lines = ["Content,Video title,Duration,Impressions,Average percentage viewed (%),Stayed to watch (%)"]
    for i in range(n):
        good = i < n // 2
        lines.append(f"v{i},#shorts 切り抜き{i}号 すごい場面 #APEX,{25 if good else 50},{5000 if good else 2000},"
                     f"{85 if good else 55},{80 if good else 50}")
    lines.append("other,関係ない動画,30,100,50,40")
    p = tmp_path / "yt.csv"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_feedback_matches_titles_and_tunes_weights(tmp_path):
    cfg = _make_ours(tmp_path)
    rows, missing = load_youtube_csv(_yt_csv(tmp_path))
    assert match_shorts(rows, our_shorts(cfg)) == 6
    assert next(r for r in rows if r.video_id == "other").matched_path == ""
    fb = analyze_feedback(cfg, rows, missing)
    assert fb.matched == 6
    # スワイプされにくかったショートはチャットが強く、音量が弱かった
    assert fb.signal_diff["chat"] > 0 > fb.signal_diff["audio"]
    assert fb.recommended["weight_chat"] > cfg.detect.weight_chat
    assert fb.recommended["weight_audio"] < cfg.detect.weight_audio
    # 短い方が維持率が高い → 最大長を短く
    assert fb.recommended["max_duration"] < cfg.detect.max_duration
    assert fb.hook_finding["with_hook"] > fb.hook_finding["without_hook"]
    md = report_markdown(fb)
    assert "スワイプ率" in md and "冒頭でスワイプされがち" in md and "関係ない動画 ※" in md
    paths = write_outputs(fb, tmp_path / "fb")
    tuned = tomllib.loads(paths["config"].read_text(encoding="utf-8"))
    assert tuned["detect"]["weight_chat"] == fb.recommended["weight_chat"]
    assert load_config([paths["config"]]).detect.max_duration == fb.recommended["max_duration"]
    json.loads(paths["json"].read_text(encoding="utf-8"))


def test_few_matches_do_not_change_weights(tmp_path):
    cfg = _make_ours(tmp_path, n=2)
    rows, missing = load_youtube_csv(_yt_csv(tmp_path, n=2))
    match_shorts(rows, our_shorts(cfg))
    fb = analyze_feedback(cfg, rows, missing)
    assert fb.recommended == {} and any("4 本未満" in n for n in fb.notes)


def test_cli_feedback(tmp_path, capsys):
    cfg = _make_ours(tmp_path)
    conf = tmp_path / "c.toml"
    conf.write_text(f'output_dir = "{cfg.output_dir}"\n')
    assert cli.main(["-c", str(conf), "feedback", str(_yt_csv(tmp_path)), "-o", str(tmp_path / "fbo")]) == 0
    out = capsys.readouterr().out
    assert "一致: 6 本" in out and "スワイプ率" in out and "推奨設定" in out
    assert (tmp_path / "fbo" / "report.md").exists()
