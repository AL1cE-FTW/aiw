import random
import tomllib
from datetime import datetime, timedelta, timezone

from twitch_shorts.analytics import analyze_channel, chat_tokens, report_markdown, write_outputs
from twitch_shorts.config import Config, load_config
from twitch_shorts.models import ChatMessage
from twitch_shorts.twitch_api import VideoInfo

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _vod_chat(seed, events):
    rng = random.Random(seed)
    chat = [ChatMessage(t + rng.random(), f"u{rng.randint(0, 300)}", rng.choice(["こんにちは", "わこつ", "なるほど"]))
            for t in range(7200) for _ in range(rng.choice([0, 1, 1, 2]))]
    for ev in events:
        for t in range(ev, ev + 12):
            for _ in range(10):
                chat.append(ChatMessage(t + rng.random(), f"v{rng.randint(0, 9999)}",
                                        rng.choice(["yuukiPog", "yuukiPog yuukiPog", "うますぎ", "草"])))
    return sorted(chat, key=lambda m: m.offset)


class FakeHelix:
    def __init__(self):
        created = NOW - timedelta(days=3)
        self.videos = {
            "v1": VideoInfo("v1", "42", "yuuki_ftw", "Yuuki", "APEX", created, 7200, "u"),
            "v2": VideoInfo("v2", "42", "yuuki_ftw", "Yuuki", "雑談", created + timedelta(days=1), 7200, "u"),
        }
        self.events = {"v1": [1000, 5000], "v2": [3000]}
        clips = []
        # 人気クリップ: 盛り上がりの直後にクリップされた (区間 = [ev-20, ev+10])
        for vid, evs in self.events.items():
            for i, ev in enumerate(evs):
                clips.append(self._clip(f"{vid}p{i}", vid, ev - 20, 30, 500 + i * 100, "509658" if vid == "v1" else "1"))
        # 不人気クリップ: 何も起きていない時間帯
        for i, off in enumerate([200, 2500, 6000]):
            clips.append(self._clip(f"low{i}", "v1", off, 30, 3, "509658"))
        clips.append(self._clip("gone", "deleted", 100, 30, 2, "1"))
        self.clips = clips

    @staticmethod
    def _clip(cid, vid, off, dur, views, game):
        return {"id": cid, "video_id": vid, "vod_offset": off, "duration": dur, "view_count": views,
                "title": f"clip {cid}", "url": f"https://clips.twitch.tv/{cid}", "game_id": game,
                "created_at": "2026-10-03T13:00:00Z"}

    def get_user_id(self, login):
        assert login == "yuuki_ftw"
        return "42"

    def get_clips(self, broadcaster_id, started_at, ended_at, max_pages=10):
        assert ended_at - started_at == timedelta(days=60)
        return self.clips

    def get_game_names(self, ids):
        return {"509658": "Apex Legends", "1": "雑談"}

    def get_video(self, vid):
        if vid not in self.videos:
            raise RuntimeError("not found")
        return self.videos[vid]


def test_analyze_channel_learns_from_popular_clips(tmp_path):
    helix = FakeHelix()
    cfg = Config(work_dir=str(tmp_path / "work"))
    fetched = []

    def fetch(vid):
        fetched.append(vid)
        return _vod_chat(int(vid[1:]), helix.events[vid])

    r = analyze_channel(cfg, helix, "yuuki_ftw", days=60, max_vods=3, chat_fetcher=fetch, now=NOW)
    assert sorted(fetched) == ["v1", "v2"]  # 削除済み VOD はスキップ
    assert r.clips[0].views == 600 and r.clips[0].game == "Apex Legends"
    # チャンネル独自のエモートが「人気の場面の言葉」として見つかる
    tokens = [k["token"] for k in r.keyword_suggestions]
    assert "yuukipog" in tokens and "こんにちは" not in tokens
    assert r.recommended["keywords"]["yuukipog"] > 0
    assert r.signal_lift["chat"] > 1.0
    # クリップ開始から約 20 秒後に盛り上がりのピーク → 前フリはそれ以上
    assert 18 <= r.recommended["pre_roll"] <= 30
    assert r.backtest["popular_clips"] == 3
    assert r.backtest["after"] >= r.backtest["before"]
    assert {x["label"] for x in r.games} == {"Apex Legends", "雑談"}
    assert any("yuukipog" in h for h in __import__("twitch_shorts.analytics").analytics.hints(r))

    # 2 回目はキャッシュを使う
    analyze_channel(cfg, helix, "yuuki_ftw", chat_fetcher=fetch, now=NOW)
    assert len(fetched) == 2

    paths = write_outputs(r, tmp_path / "a")
    md = paths["report"].read_text(encoding="utf-8")
    assert "クリップ分析レポート" in md and "yuukipog" in md and "答え合わせ" in md
    tuned = tomllib.loads(paths["config"].read_text(encoding="utf-8"))
    assert tuned["detect"]["keywords"]["yuukipog"] > 0
    # 推奨設定はそのまま設定ファイルとして読み込める
    loaded = load_config(paths["config"])
    assert "yuukipog" in loaded.detect.keywords and "草" in loaded.detect.keywords


def test_analyze_with_no_clips_still_reports(tmp_path):
    helix = FakeHelix()
    helix.clips = []
    r = analyze_channel(Config(work_dir=str(tmp_path)), helix, "yuuki_ftw", chat_fetcher=lambda v: [], now=NOW)
    md = report_markdown(r)
    assert r.clips == [] and r.backtest == {} and r.recommended == {}
    assert "データが少ない" in md


def test_chat_tokens():
    assert chat_tokens(ChatMessage(0, "a", "wwwwwww")) == {"www"}
    assert chat_tokens(ChatMessage(0, "a", "yuukiPog yuukiPog GG", ["yuukiPog"])) == {"yuukipog", "gg"}
    assert chat_tokens(ChatMessage(0, "a", "これは長い文章なので一語としては扱わない")) == set()
