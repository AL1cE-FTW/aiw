"""チャンネルの「よく見られたクリップ」を分析し、ショート作りの参考情報と調整済み設定を作る。

Twitch のダッシュボード分析は API で取得できないため、公式 API で取れる
「視聴者が作ったクリップとその再生数」を、"みんなが見たがる場面" の実績データとして使う。

分析内容:
  1. 人気クリップの傾向 (長さ・配信内の位置・ゲーム・曜日/時間帯)
  2. 人気クリップの場面で、どのシグナル (チャット流速/盛り上がりワード/視聴者数…) が強く出ていたか
     → シグナルの重みを調整
  3. 人気クリップの場面のチャットに特徴的な言葉 (チャンネル独自のエモート・定番コメント)
     → 盛り上がりワードに追加
  4. 盛り上がりのピークがクリップのどこにあるか → pre_roll / post_roll を調整
  5. 答え合わせ: 現在の設定と調整後の設定で、人気クリップの場面をどれだけ検出できるか
"""

from __future__ import annotations

import bisect
import copy
import json
import logging
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np

from .chat import load_chat, save_chat_jsonl
from .config import Config, DetectConfig
from .detector import build_signals, combine_signals, detect_highlights
from .models import ChatMessage
from .twitch_api import VideoInfo, parse_rfc3339

log = logging.getLogger(__name__)

JST = ZoneInfo("Asia/Tokyo")
WEEKDAYS = "月火水木金土日"


@dataclass
class ClipStat:
    id: str
    title: str
    views: int
    duration: float
    created_at: str
    url: str
    game: str = ""
    video_id: str = ""
    vod_offset: float | None = None
    position: float | None = None  # 配信内の位置 (0=開始, 1=終了)
    signals: dict[str, float] = field(default_factory=dict)  # クリップ区間での各シグナルの最大値
    peak_from_start: float | None = None  # クリップ開始から盛り上がりピークまでの秒数
    detected_before: bool | None = None
    detected_after: bool | None = None


@dataclass
class AnalysisResult:
    channel: str
    period_days: int
    generated_at: str
    clips: list[ClipStat]
    analyzed_vods: list[str]
    duration_buckets: list[dict]
    position_buckets: list[dict]
    games: list[dict]
    time_slots: list[dict]
    signal_lift: dict[str, float]
    keyword_suggestions: list[dict]
    recommended: dict
    backtest: dict
    current: dict = field(default_factory=dict)  # 推奨値を出した項目の現在の値
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 集計ヘルパー
# ---------------------------------------------------------------------------

def _bucket_table(items: list[tuple[str, int]], order: list[str] | None = None) -> list[dict]:
    """(ラベル, 再生数) の一覧を、ラベルごとの本数・中央値・合計にまとめる。"""
    groups: dict[str, list[int]] = {}
    for label, views in items:
        groups.setdefault(label, []).append(views)
    labels = order if order else sorted(groups, key=lambda k: -statistics.median(groups[k]))
    return [
        {"label": k, "count": len(groups[k]), "median_views": statistics.median(groups[k]),
         "total_views": sum(groups[k])}
        for k in labels if k in groups
    ]


def _duration_label(d: float) -> str:
    # Twitch のクリップは既定 30 秒なので、境界値は短い側に含める
    if d <= 15:
        return "〜15秒"
    if d <= 30:
        return "16〜30秒"
    if d <= 45:
        return "31〜45秒"
    return "46〜60秒"


def _position_label(p: float) -> str:
    if p < 1 / 3:
        return "序盤"
    if p < 2 / 3:
        return "中盤"
    return "終盤"


def _time_label(dt: datetime) -> str:
    local = dt.astimezone(JST)
    h = local.hour
    slot = "深夜(0-6時)" if h < 6 else "朝昼(6-17時)" if h < 17 else "夜(17-21時)" if h < 21 else "夜遅め(21-24時)"
    kind = "土日" if local.weekday() >= 5 else "平日"
    return f"{kind} {slot}"


_REPEAT_RE = re.compile(r"(.)\1{2,}")


def chat_tokens(m: ChatMessage) -> set[str]:
    """チャット 1 件から特徴語を取り出す (エモート名・空白区切りの語・短い一言)。"""
    tokens = {e.lower() for e in m.emotes if e}
    text = _REPEAT_RE.sub(lambda x: x.group(1) * 3, m.text.strip().lower())  # wwwww → www
    words = text.split()
    if len(words) > 1:
        tokens.update(w for w in words if 1 <= len(w) <= 20)
    elif text and len(text) <= 12:
        tokens.add(text)
    return tokens


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def analyze_channel(
    cfg: Config,
    helix,
    channel: str,
    days: int = 60,
    max_vods: int = 3,
    chat_fetcher: Callable[[str], list[ChatMessage]] | None = None,
    now: datetime | None = None,
) -> AnalysisResult:
    from .chat import fetch_vod_chat

    chat_fetcher = chat_fetcher or fetch_vod_chat
    now = now or datetime.now(timezone.utc)
    user_id = helix.get_user_id(channel)
    raw = helix.get_clips(user_id, now - timedelta(days=days), now)
    log.info("期間内のクリップ %d 件", len(raw))
    games = helix.get_game_names([c.get("game_id", "") for c in raw]) if raw else {}
    clips = [
        ClipStat(
            id=c["id"], title=c.get("title", ""), views=int(c.get("view_count") or 0),
            duration=float(c.get("duration") or 30), created_at=c.get("created_at", ""), url=c.get("url", ""),
            game=games.get(c.get("game_id", ""), ""), video_id=c.get("video_id") or "",
            vod_offset=float(c["vod_offset"]) if c.get("vod_offset") is not None else None,
        )
        for c in raw
    ]
    clips.sort(key=lambda c: c.views, reverse=True)
    notes: list[str] = []
    if len(clips) < 5:
        notes.append("クリップが少ないため傾向は参考程度です。期間 (--days) を伸ばすと精度が上がります。")

    # --- VOD ごとの分析 (チャットが必要) -----------------------------------
    by_vod: dict[str, list[ClipStat]] = {}
    for c in clips:
        if c.video_id and c.vod_offset is not None:
            by_vod.setdefault(c.video_id, []).append(c)
    vod_order = sorted(by_vod, key=lambda v: -sum(c.views for c in by_vod[v]))
    analyzed: list[str] = []
    vod_data: list[tuple[VideoInfo, list[ChatMessage], list[ClipStat]]] = []
    for vid in vod_order:
        if len(analyzed) >= max_vods:
            break
        try:
            video = helix.get_video(vid)
            chat = _cached_chat(cfg, vid, chat_fetcher)
        except Exception as e:
            log.warning("VOD %s を分析できませんでした (削除済み等): %s", vid, e)
            continue
        for c in by_vod[vid]:
            c.position = min(1.0, (c.vod_offset or 0) / max(video.duration, 1))
        vod_data.append((video, chat, by_vod[vid]))
        analyzed.append(vid)
    if not analyzed and by_vod:
        notes.append("クリップ元の VOD が残っていないため、チャットの分析はできませんでした。")

    signal_lift, keyword_suggestions, peaks = _moment_analysis(cfg.detect, vod_data, clips)
    recommended = _recommend(cfg.detect, signal_lift, keyword_suggestions, peaks)
    backtest = _backtest(cfg.detect, recommended, vod_data, clips)

    def _views(cs, key):
        return [(key(c), c.views) for c in cs]

    return AnalysisResult(
        channel=channel,
        period_days=days,
        generated_at=now.astimezone(JST).isoformat(timespec="minutes"),
        clips=clips,
        analyzed_vods=analyzed,
        duration_buckets=_bucket_table(_views(clips, lambda c: _duration_label(c.duration)),
                                       ["〜15秒", "16〜30秒", "31〜45秒", "46〜60秒"]),
        position_buckets=_bucket_table(_views([c for c in clips if c.position is not None],
                                              lambda c: _position_label(c.position)), ["序盤", "中盤", "終盤"]),
        games=_bucket_table(_views([c for c in clips if c.game], lambda c: c.game)),
        time_slots=_bucket_table(_views([c for c in clips if c.created_at],
                                        lambda c: _time_label(parse_rfc3339(c.created_at)))),
        signal_lift=signal_lift,
        keyword_suggestions=keyword_suggestions,
        recommended=recommended,
        backtest=backtest,
        current={k: getattr(cfg.detect, k) for k in recommended if k != "keywords"},
        notes=notes,
    )


def _cached_chat(cfg: Config, video_id: str, fetcher) -> list[ChatMessage]:
    cache = Path(cfg.work_dir) / f"vod_{video_id}" / "chat.jsonl"
    if cache.exists():
        return load_chat(cache)
    log.info("VOD %s のチャットを取得中…", video_id)
    chat = fetcher(video_id)
    cache.parent.mkdir(parents=True, exist_ok=True)
    save_chat_jsonl(chat, cache)
    return chat


def _popular(clips: list[ClipStat]) -> set[str]:
    """再生数の上位 25% (最低 3 本) のクリップを「人気」とみなす。"""
    ranked = sorted((c for c in clips if c.views > 0), key=lambda c: c.views, reverse=True)
    k = max(3, len(ranked) // 4)
    return {c.id for c in ranked[:k]}


def _moment_analysis(dcfg: DetectConfig, vod_data, all_clips):
    popular = _popular(all_clips)
    lift_samples: dict[str, list[float]] = {}
    base_samples: dict[str, list[float]] = {}
    pop_tokens: Counter = Counter()
    all_tokens: Counter = Counter()
    pop_msgs = 0
    total_msgs = 0
    peaks: list[tuple[float, float]] = []  # (ピークまでの秒数, ピーク後の秒数)
    rng = np.random.default_rng(0)

    for video, chat, vclips in vod_data:
        signals = build_signals(video.duration, chat, dcfg)
        _, normed = combine_signals(signals, dcfg)
        n = len(next(iter(normed.values())))
        # 比較用: クリップと無関係なランダムな時間帯の値
        for name, z in normed.items():
            idx = rng.integers(0, n, size=min(200, n))
            base_samples.setdefault(name, []).extend(float(z[i]) for i in idx)
        for m in chat:
            all_tokens.update(chat_tokens(m))
        total_msgs += len(chat)
        offsets = [m.offset for m in chat]
        for c in vclips:
            # Twitch のクリップは「クリップした瞬間の直前」を切り取るので、区間 = [offset, offset+duration]
            a = max(0, int(c.vod_offset))
            b = min(n, int(c.vod_offset + c.duration) + 1)
            if b <= a:
                continue
            c.signals = {name: round(float(z[a:b].max()), 2) for name, z in normed.items()}
            if c.id not in popular:
                continue
            for name, v in c.signals.items():
                lift_samples.setdefault(name, []).append(v)
            chat_z = normed.get("chat")
            if chat_z is not None:
                # 盛り上がりが数秒続くので、最大値の位置ではなく反応の重心をピークとする
                w = np.clip(chat_z[a:b], 0, None) ** 2
                peak = a + (float(np.average(np.arange(b - a), weights=w)) if w.sum() > 0 else float(np.argmax(chat_z[a:b])))
                c.peak_from_start = round(peak - c.vod_offset, 1)
                peaks.append((c.peak_from_start, c.vod_offset + c.duration - peak))
            # 人気クリップ区間 (+ チャットの反応遅れ) のチャット
            lo = bisect.bisect_left(offsets, c.vod_offset)
            hi = bisect.bisect_left(offsets, c.vod_offset + c.duration + dcfg.chat_delay)
            for m in chat[lo:hi]:
                pop_tokens.update(chat_tokens(m))
            pop_msgs += hi - lo

    lift = {
        name: round(float(np.mean(lift_samples[name]) - np.mean(base_samples.get(name, [0.0]))), 2)
        for name in lift_samples
    }

    suggestions = []
    if pop_msgs and total_msgs:
        existing = set(dcfg.keywords)
        for tok, cnt in pop_tokens.most_common(200):
            if cnt < 5 or tok in existing:
                continue
            rate_pop = cnt / pop_msgs
            rate_all = all_tokens[tok] / total_msgs
            ratio = rate_pop / max(rate_all, 1e-9)
            if ratio >= 2.0:
                suggestions.append({"token": tok, "count": cnt, "lift": round(ratio, 1),
                                    "weight": round(min(1.5, 0.5 + 0.1 * ratio), 1)})
        suggestions = sorted(suggestions, key=lambda s: -s["lift"] * np.log1p(s["count"]))[:15]
    return lift, suggestions, peaks


def _recommend(dcfg: DetectConfig, lift: dict[str, float], keywords: list[dict], peaks) -> dict:
    rec: dict = {}
    weight_keys = {"chat": "weight_chat", "keywords": "weight_keywords", "audio": "weight_audio",
                   "viewers": "weight_viewers"}
    usable = {k: v for k, v in lift.items() if k in weight_keys}
    if usable:
        mean_lift = float(np.mean([max(v, 0.0) for v in usable.values()])) or 1.0
        for name, v in usable.items():
            base = getattr(dcfg, weight_keys[name])
            # 人気の場面でよく反応していたシグナルほど重くする (急な変化を避けて 0.5〜1.5 倍)
            factor = min(1.5, max(0.5, max(v, 0.0) / mean_lift))
            new = round(base * factor, 2)
            if abs(new - base) >= 0.05:
                rec[weight_keys[name]] = new
    if len(peaks) >= 3:
        pre = statistics.median(p[0] for p in peaks)
        post = statistics.median(p[1] for p in peaks)
        rec["pre_roll"] = round(min(30.0, max(8.0, pre + 3)), 1)  # 前フリを少し長めに
        rec["post_roll"] = round(min(20.0, max(4.0, post + 2)), 1)
    if keywords:
        rec["keywords"] = {k["token"]: k["weight"] for k in keywords}
    return rec


def tuned_detect_config(dcfg: DetectConfig, recommended: dict) -> DetectConfig:
    new = copy.deepcopy(dcfg)
    for k, v in recommended.items():
        if k == "keywords":
            new.keywords = {**new.keywords, **{t.lower(): w for t, w in v.items()}}
        else:
            setattr(new, k, v)
    return new


def _backtest(dcfg: DetectConfig, recommended: dict, vod_data, all_clips) -> dict:
    """人気クリップの場面を、各設定の検出結果がどれだけ含んでいたか (再現率)。"""
    popular = _popular(all_clips)
    tuned = tuned_detect_config(dcfg, recommended)
    hits = {"before": 0, "after": 0}
    total = 0
    for video, chat, vclips in vod_data:
        targets = [c for c in vclips if c.id in popular]
        if not targets:
            continue
        results = {}
        for label, d in (("before", dcfg), ("after", tuned)):
            hs, _ = detect_highlights(video.duration, chat, d)
            results[label] = hs
        for c in targets:
            total += 1
            center = (c.vod_offset or 0) + c.duration * 0.6
            for label, hs in results.items():
                hit = any(h.start <= center <= h.end for h in hs)
                hits[label] += hit
                setattr(c, f"detected_{label}", hit)
    if not total:
        return {}
    return {"popular_clips": total, "before": hits["before"], "after": hits["after"]}


# ---------------------------------------------------------------------------
# 出力
# ---------------------------------------------------------------------------

def _toml_key(k: str) -> str:
    return json.dumps(k, ensure_ascii=False)


def overrides_toml(header: str, rec: dict, current: dict) -> str:
    """推奨値 (detect セクションの上書き) を、そのまま -c で重ねられる TOML にする。"""
    lines = [f"# {header}", "# 使い方: twitch-shorts -c config.toml -c <このファイル> ...", "", "[detect]"]
    for k, v in rec.items():
        if k != "keywords":
            cur = current.get(k)
            lines.append(f"{k} = {v}" + (f"  # 現在: {cur}" if cur is not None else ""))
    if rec.get("keywords"):
        lines += ["", "[detect.keywords]"]
        lines += [f"{_toml_key(k)} = {v}" for k, v in rec["keywords"].items()]
    return "\n".join(lines) + "\n"


def tuned_config_toml(result: AnalysisResult) -> str:
    return overrides_toml(f"{result.channel} のクリップ分析 ({result.generated_at}) から作った推奨設定",
                          result.recommended, result.current)


SIGNAL_NAMES = {"chat": "チャット流速", "keywords": "盛り上がりワード", "audio": "音量",
                "clips": "既存クリップ", "viewers": "同時視聴者数の増加"}


def report_markdown(result: AnalysisResult) -> str:
    r = result
    out = [f"# {r.channel} クリップ分析レポート", "",
           f"- 対象期間: 直近 {r.period_days} 日 / 作成: {r.generated_at}",
           f"- クリップ数: {len(r.clips)} 本 / 総再生数: {sum(c.views for c in r.clips):,}",
           f"- チャットまで分析した VOD: {', '.join(r.analyzed_vods) or 'なし'}", ""]
    if r.notes:
        out += ["> " + n for n in r.notes] + [""]

    out += ["## よく見られているクリップ (上位 10)", "",
            "| 再生数 | 長さ | ゲーム | タイトル |", "| ---: | ---: | --- | --- |"]
    for c in r.clips[:10]:
        title = c.title.replace("|", "｜")
        out.append(f"| {c.views:,} | {c.duration:.0f}秒 | {c.game} | [{title}]({c.url}) |")

    def table(title, rows, note=""):
        if not rows:
            return []
        t = [f"## {title}", ""]
        if note:
            t += [note, ""]
        t += ["| 区分 | 本数 | 再生数(中央値) | 再生数(合計) |", "| --- | ---: | ---: | ---: |"]
        t += [f"| {x['label']} | {x['count']} | {x['median_views']:,.0f} | {x['total_views']:,} |" for x in rows]
        return t + [""]

    out += [""]
    out += table("長さ別", r.duration_buckets)
    out += table("配信内の位置別", r.position_buckets, "配信のどのあたりの場面がよく見られているか。")
    out += table("ゲーム・カテゴリ別", r.games)
    out += table("曜日・時間帯別 (JST)", r.time_slots, "クリップが作られた時間帯 ≒ 配信していた時間帯。")

    if r.signal_lift:
        out += ["## 人気クリップの場面で強く出ていたシグナル", "",
                "ランダムな時間帯と比べて、人気クリップの場面でどれだけ高かったか (大きいほど人気の場面と関係が深い)。", "",
                "| シグナル | 差 |", "| --- | ---: |"]
        out += [f"| {SIGNAL_NAMES.get(k, k)} | {v:+.2f} |"
                for k, v in sorted(r.signal_lift.items(), key=lambda x: -x[1])]
        out += [""]
    if r.keyword_suggestions:
        out += ["## 人気の場面に特徴的なチャット", "",
                "普段より人気クリップの場面で多く出ていた言葉・エモート。盛り上がりワードに追加を推奨します。", "",
                "| 言葉 | 出現数 | 普段の何倍 | 推奨の重み |", "| --- | ---: | ---: | ---: |"]
        out += [f"| {k['token']} | {k['count']} | {k['lift']}倍 | {k['weight']} |" for k in r.keyword_suggestions]
        out += [""]
    if r.backtest:
        b = r.backtest
        out += ["## 答え合わせ (人気クリップの場面を検出できたか)", "",
                f"- 現在の設定: {b['before']} / {b['popular_clips']} 本",
                f"- 推奨設定: {b['after']} / {b['popular_clips']} 本",
                "", "※ 同じデータで調整・評価しているため、実際の効果はこれより小さい可能性があります。", ""]

    out += ["## 動画づくりのヒント", ""] + [f"- {h}" for h in hints(r)] + [""]
    if r.recommended:
        out += ["## 推奨設定", "", "`config.tuned.toml` に書き出しました。", "", "```toml",
                tuned_config_toml(r).rstrip(), "```", ""]
    return "\n".join(out)


def hints(r: AnalysisResult) -> list[str]:
    """集計結果から、次の動画づくりに使える具体的な示唆を文章にする。"""
    tips: list[str] = []

    def best(rows, min_count=2, margin=1.5):
        """本数が十分あり、2 位より明確に (margin 倍以上) 多く見られている区分だけを返す。"""
        rows = sorted((x for x in rows if x["count"] >= min_count), key=lambda x: -x["median_views"])
        if len(rows) < 2 or rows[0]["median_views"] < margin * max(rows[1]["median_views"], 1):
            return None
        return rows[0]

    if (b := best(r.duration_buckets)):
        tips.append(f"{b['label']}のクリップが最もよく見られています (中央値 {b['median_views']:,.0f} 回)。"
                    "ショートの長さもこれに近づけるのがおすすめです。")
    if (b := best(r.position_buckets)):
        tips.append(f"配信の{b['label']}の場面が人気です。{b['label']}は特に注意して切り抜き候補を確認しましょう。")
    if (b := best(r.games)):
        tips.append(f"「{b['label']}」の配信のクリップが最もよく見られています。このカテゴリのショートを優先すると効果的です。")
    if (b := best(r.time_slots)):
        tips.append(f"{b['label']}の配信から人気クリップが生まれやすい傾向があります。")
    if r.signal_lift:
        name, _ = max(r.signal_lift.items(), key=lambda x: x[1])
        tip = f"人気の場面は「{SIGNAL_NAMES.get(name, name)}」が特に強く出ていました。"
        key = f"weight_{name}"
        if key in r.recommended and r.recommended[key] > r.current.get(key, 0):
            tip += f"推奨設定ではこの重みを {r.current[key]} → {r.recommended[key]} に上げています。"
        tips.append(tip)
    if r.keyword_suggestions:
        words = "、".join(k["token"] for k in r.keyword_suggestions[:5])
        tips.append(f"人気の場面ではチャットに「{words}」が多く出ています。視聴者がこう反応する場面が"
                    "このチャンネルの見どころです。")
    if not tips:
        tips.append("データが少ないため具体的な傾向はまだ出ていません。配信とクリップが増えたら再分析してください。")
    return tips


def write_outputs(result: AnalysisResult, out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "report": out_dir / "report.md",
        "json": out_dir / "analysis.json",
        "config": out_dir / "config.tuned.toml",
    }
    paths["report"].write_text(report_markdown(result), encoding="utf-8")
    paths["json"].write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    paths["config"].write_text(tuned_config_toml(result), encoding="utf-8")
    return paths
