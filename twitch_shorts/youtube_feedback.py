"""YouTube Studio の数値を取り込み、投稿したショートの結果から検出設定を調整する。

参考: タルレミ・エラ「OW動画投稿講座」— ショートで見るべき指標はインプレッション・スワイプ率・維持率。
YouTube Studio の [アナリティクス] → [詳細モード] → [現在のビューをエクスポート] で書き出した
``Table data.csv`` を読み込む。列名は英語/日本語の表記ゆれを吸収する。エクスポートにショートの
「視聴を継続」が含まれない場合は、ショートの画面の数値を書き写した CSV
(列: title, impressions, stayed_pct, avg_viewed_pct) でもよい。

作ったショートとは「タイトル」で突き合わせる (投稿時に付けたハッシュタグ等は無視)。
"""

from __future__ import annotations

import csv
import difflib
import io
import json
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .analytics import SIGNAL_NAMES, overrides_toml
from .config import Config
from .fileutil import read_json
from .schedule import load_schedule

# 列名の候補 (小文字・空白正規化後)。完全一致を優先し、無ければ部分一致で探す
COLUMN_ALIASES: dict[str, list[str]] = {
    "video_id": ["content", "コンテンツ", "video id", "動画 id", "動画id", "video_id"],
    "title": ["video title", "動画のタイトル", "タイトル", "title"],
    "duration": ["duration", "長さ", "動画の長さ"],
    "views": ["views", "視聴回数", "再生回数"],
    "impressions": ["impressions", "インプレッション数", "インプレッション"],
    "stayed_pct": ["stayed to watch", "viewed vs. swiped away", "viewed vs swiped away", "視聴を継続",
                   "スワイプせずに視聴", "stayed_pct"],
    "avg_viewed_pct": ["average percentage viewed", "平均視聴率", "平均再生率", "avg_viewed_pct"],
}
# 部分一致で拾ってはいけない列 (例: "Impressions click-through rate" は impressions ではない)
EXCLUDE_WORDS: dict[str, list[str]] = {
    "impressions": ["click-through", "クリック率", "ctr"],
    "views": ["unique", "ユニーク", "engaged", "エンゲージ", "duration", "時間", "percentage", "率"],
    "duration": ["average", "平均", "watch time", "総再生時間"],
}
TOTAL_ROWS = {"total", "合計"}


@dataclass
class ShortResult:
    title: str
    video_id: str = ""
    views: float | None = None
    impressions: float | None = None
    stayed_pct: float | None = None  # スワイプされずに視聴された割合 (= 100 - スワイプ率)
    avg_viewed_pct: float | None = None  # 平均視聴率 (維持率)
    duration: float | None = None
    matched_path: str = ""
    signals: dict[str, float] = field(default_factory=dict)
    hook: str = ""
    our_duration: float | None = None


@dataclass
class FeedbackResult:
    generated_at: str
    shorts: list[ShortResult]
    matched: int
    benchmarks: dict
    signal_diff: dict[str, float]
    duration_finding: dict
    hook_finding: dict
    recommended: dict
    current: dict
    missing_columns: list[str]
    notes: list[str] = field(default_factory=list)


def _norm_header(h: str) -> str:
    h = h.replace("﻿", "").strip().lower()
    h = re.sub(r"\s*[\(（][^)）]*[\)）]\s*$", "", h)  # 末尾の "(%)" などを外す
    return re.sub(r"\s+", " ", h)


def map_columns(headers: list[str]) -> dict[str, str]:
    normed = {h: _norm_header(h) for h in headers}
    mapping: dict[str, str] = {}
    for key, aliases in COLUMN_ALIASES.items():
        exact = [h for h, n in normed.items() if n in aliases]
        if exact:
            mapping[key] = exact[0]
            continue
        partial = [h for h, n in normed.items()
                   if any(a in n for a in aliases) and not any(x in n for x in EXCLUDE_WORDS.get(key, []))]
        if partial:
            mapping[key] = min(partial, key=len)
    return mapping


def _num(value: str | None) -> float | None:
    if value is None:
        return None
    v = value.strip().replace(",", "").replace("%", "")
    if not v or v in {"-", "—"}:
        return None
    # "0:35" のような時間表記
    if ":" in v:
        parts = [float(p) for p in v.split(":")]
        total = 0.0
        for p in parts:
            total = total * 60 + p
        return total
    try:
        return float(v)
    except ValueError:
        return None


def load_youtube_csv(path: str | Path) -> tuple[list[ShortResult], list[str]]:
    """CSV を読み込み、(ショートごとの結果, 見つからなかった指標名) を返す。"""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        return [], list(COLUMN_ALIASES)
    cols = map_columns(list(rows[0].keys()))
    if "title" not in cols and "video_id" not in cols:
        raise ValueError(f"タイトル列が見つかりません。列: {list(rows[0].keys())}")
    out: list[ShortResult] = []
    for r in rows:
        title = (r.get(cols.get("title", ""), "") or "").strip()
        vid = (r.get(cols.get("video_id", ""), "") or "").strip()
        if title.lower() in TOTAL_ROWS or vid.lower() in TOTAL_ROWS or not (title or vid):
            continue
        res = ShortResult(title=title, video_id=vid)
        for key in ("views", "impressions", "stayed_pct", "avg_viewed_pct", "duration"):
            if key in cols:
                setattr(res, key, _num(r.get(cols[key])))
        # ショート以外 (3 分超) は除外
        if res.duration is not None and res.duration > 180:
            continue
        out.append(res)
    missing = [k for k in ("impressions", "stayed_pct", "avg_viewed_pct") if k not in cols]
    return out, missing


# ---------------------------------------------------------------------------
# 作ったショートとの突き合わせ
# ---------------------------------------------------------------------------

def _norm_title(t: str) -> str:
    # ハッシュタグ (#shorts 等) は消すが、「切り抜き #12」のような番号は区別に必要なので残す
    t = re.sub(r"#(?!\d+(?:\s|$))\S+", "", t)
    t = re.sub(r"[\s　【】\[\]()（）「」『』!！?？・|｜]+", "", t)
    return t.lower()


def our_shorts(cfg: Config) -> list[dict]:
    """これまでに作ったショート (投稿予定表 + 各実行の highlights.json)。"""
    items: dict[str, dict] = {}
    out = Path(cfg.output_dir)
    for hj in out.glob("**/highlights.json"):
        data = read_json(hj, {})
        items_ = data.get("highlights", []) if isinstance(data, dict) else []
        for h in items_ if isinstance(items_, list) else []:
            if isinstance(h, dict) and isinstance(h.get("output_path"), str) and isinstance(h.get("title"), str) \
                    and h["output_path"] and h["title"]:
                items[h["output_path"]] = {"title": h["title"], "path": h["output_path"],
                                           "signals": h["signals"] if isinstance(h.get("signals"), dict) else {},
                                           "hook": h.get("hook", ""),
                                           "duration": h.get("video_duration") or h.get("duration")}
    for e in load_schedule(cfg):
        if not e["title"]:  # タイトルが無いと YouTube の結果と突き合わせられない
            continue
        items.setdefault(e["path"], {"title": e["title"], "path": e["path"], "signals": e.get("signals", {}),
                                     "hook": e.get("hook", ""), "duration": e.get("duration")})
    return list(items.values())


def match_shorts(results: list[ShortResult], ours: list[dict], threshold: float = 0.75) -> int:
    pool = [(o, _norm_title(o["title"])) for o in ours]
    used: set[str] = set()
    n = 0
    # 似たタイトルが多い場合に取り違えないよう、タイトルが完全に一致するものから先に確定させる
    by_key: dict[str, list[dict]] = {}
    for o, okey in pool:
        by_key.setdefault(okey, []).append(o)
    for r in results:
        cands = [o for o in by_key.get(_norm_title(r.title), []) if o["path"] not in used]
        if cands:
            _attach(r, cands[0])
            used.add(cands[0]["path"])
            n += 1
    for r in results:
        if r.matched_path:
            continue
        key = _norm_title(r.title)
        if not key:
            continue
        best, best_ratio = None, 0.0
        for o, okey in pool:
            if o["path"] in used or not okey:
                continue
            if okey == key:
                ratio = 1.0
            elif (okey in key or key in okey) and min(len(okey), len(key)) / max(len(okey), len(key)) >= 0.8:
                ratio = 0.95  # 投稿時に少し書き足した程度
            else:
                ratio = difflib.SequenceMatcher(None, key, okey).ratio()
            if ratio > best_ratio:
                best, best_ratio = o, ratio
        if best and best_ratio >= threshold:
            used.add(best["path"])
            _attach(r, best)
            n += 1
    return n


def _attach(r: ShortResult, o: dict) -> None:
    r.matched_path = o["path"]
    r.signals = o.get("signals") or {}
    r.hook = o.get("hook") or ""
    r.our_duration = o.get("duration")


# ---------------------------------------------------------------------------
# 分析
# ---------------------------------------------------------------------------

def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def analyze_feedback(cfg: Config, results: list[ShortResult], missing: list[str]) -> FeedbackResult:
    matched = [r for r in results if r.matched_path]
    notes: list[str] = []
    if "stayed_pct" in missing:
        notes.append("CSV に「視聴を継続 (Stayed to watch / Viewed vs. swiped away)」の列がありません。"
                     "詳細モードで列を追加してからエクスポートするか、ショートの画面の数値を書き写した CSV を使ってください。")
    stayed = _median(r.stayed_pct for r in results)
    benchmarks = {
        "shorts": len(results),
        "median_impressions": _median(r.impressions for r in results),
        "median_stayed_pct": stayed,
        "median_swipe_pct": round(100 - stayed, 1) if stayed is not None else None,
        "median_avg_viewed_pct": _median(r.avg_viewed_pct for r in results),
    }

    # 1) スワイプされにくかったショートで強かったシグナル → 重みを調整
    signal_diff: dict[str, float] = {}
    rated = [r for r in matched if r.stayed_pct is not None and r.signals]
    if len(rated) >= 4:
        med = statistics.median(r.stayed_pct for r in rated)
        good = [r for r in rated if r.stayed_pct >= med]
        bad = [r for r in rated if r.stayed_pct < med]
        if good and bad:
            names = set().union(*(r.signals for r in rated))
            for name in names:
                g = [r.signals.get(name, 0.0) for r in good]
                b = [r.signals.get(name, 0.0) for r in bad]
                signal_diff[name] = round(statistics.mean(g) - statistics.mean(b), 2)
    else:
        notes.append("作ったショートとの突き合わせが 4 本未満のため、検出の重みは調整していません。")

    # 2) 長さと維持率
    duration_finding: dict = {}
    timed = [r for r in matched if r.avg_viewed_pct is not None and r.our_duration]
    if len(timed) >= 4:
        dmed = statistics.median(r.our_duration for r in timed)
        short = [r.avg_viewed_pct for r in timed if r.our_duration <= dmed]
        long_ = [r.avg_viewed_pct for r in timed if r.our_duration > dmed]
        if short and long_:
            duration_finding = {"split_seconds": round(dmed, 1),
                                "shorter_avg_viewed": round(statistics.mean(short), 1),
                                "longer_avg_viewed": round(statistics.mean(long_), 1)}

    # 3) 引きの言葉の有無とスワイプ
    hook_finding: dict = {}
    with_hook = [r.stayed_pct for r in matched if r.stayed_pct is not None and r.hook]
    without = [r.stayed_pct for r in matched if r.stayed_pct is not None and not r.hook]
    if len(with_hook) >= 2 and len(without) >= 2:
        hook_finding = {"with_hook": round(statistics.mean(with_hook), 1),
                        "without_hook": round(statistics.mean(without), 1)}

    recommended: dict = {}
    current: dict = {}
    weight_keys = {"chat": "weight_chat", "keywords": "weight_keywords", "audio": "weight_audio",
                   "viewers": "weight_viewers", "clips": "weight_clips"}
    for name, diff in signal_diff.items():
        key = weight_keys.get(name)
        if not key:
            continue
        base = getattr(cfg.detect, key)
        # スコア 1 点の差ごとに 10% 調整 (±30% まで)。少ないデータで振れすぎないようにする
        new = round(base * min(1.3, max(0.7, 1 + 0.1 * diff)), 2)
        if abs(new - base) >= 0.05:
            recommended[key], current[key] = new, base
    if duration_finding and duration_finding["shorter_avg_viewed"] - duration_finding["longer_avg_viewed"] >= 5:
        new_max = max(cfg.detect.min_duration + 5, round(duration_finding["split_seconds"] + 5))
        if new_max < cfg.detect.max_duration:
            recommended["max_duration"], current["max_duration"] = float(new_max), cfg.detect.max_duration

    return FeedbackResult(
        generated_at=datetime.now().isoformat(timespec="minutes"),
        shorts=results, matched=len(matched), benchmarks=benchmarks, signal_diff=signal_diff,
        duration_finding=duration_finding, hook_finding=hook_finding, recommended=recommended,
        current=current, missing_columns=missing, notes=notes,
    )


def _diagnose(r: ShortResult, bench: dict) -> str:
    """チャンネルの中央値と比べた、そのショートの課題。"""
    issues = []
    if r.stayed_pct is not None and bench.get("median_stayed_pct") is not None \
            and r.stayed_pct < bench["median_stayed_pct"] - 5:
        issues.append("冒頭でスワイプされがち (最初の 3 秒の引きを強く)")
    if r.avg_viewed_pct is not None and bench.get("median_avg_viewed_pct") is not None \
            and r.avg_viewed_pct < bench["median_avg_viewed_pct"] - 5:
        issues.append("途中で離脱されがち (短く・テンポよく)")
    if r.impressions is not None and bench.get("median_impressions") is not None \
            and r.impressions < bench["median_impressions"] * 0.5:
        issues.append("表示回数が少ない")
    return " / ".join(issues) or "良好"


def report_markdown(fb: FeedbackResult) -> str:
    b = fb.benchmarks
    out = ["# YouTube ショートの結果レポート", "",
           f"- 作成: {fb.generated_at}",
           f"- 読み込んだショート: {b['shorts']} 本 (このツールで作ったものと一致: {fb.matched} 本)", "",
           "## チャンネルの基準値 (中央値)", "",
           "講座で重視されている 3 指標です。外部の目安 (維持率 60% など) より、自分のチャンネルの中央値と比べるのが確実です。", "",
           "| 指標 | 中央値 |", "| --- | ---: |",
           f"| インプレッション | {b['median_impressions'] if b['median_impressions'] is not None else '—'} |",
           f"| 視聴を継続 (スワイプされなかった割合) | {_pct(b['median_stayed_pct'])} |",
           f"| スワイプ率 | {_pct(b['median_swipe_pct'])} |",
           f"| 平均視聴率 (維持率) | {_pct(b['median_avg_viewed_pct'])} |", ""]
    if fb.notes:
        out += ["> " + n for n in fb.notes] + [""]
    out += ["## ショートごとの結果", "",
            "| タイトル | インプレッション | 視聴を継続 | 平均視聴率 | 診断 |", "| --- | ---: | ---: | ---: | --- |"]
    ordered = sorted(fb.shorts, key=lambda r: -(r.stayed_pct or 0))
    for r in ordered:
        mark = "" if r.matched_path else " ※"
        out.append(f"| {r.title.replace('|', '｜')}{mark} | {r.impressions if r.impressions is not None else '—'} | "
                   f"{_pct(r.stayed_pct)} | {_pct(r.avg_viewed_pct)} | {_diagnose(r, b)} |")
    out += ["", "※ = このツールで作ったショートと一致しなかったもの (基準値の計算にだけ使用)", ""]

    if fb.signal_diff:
        out += ["## スワイプされにくかったショートの特徴", "",
                "視聴を継続の割合が中央値以上のショートと未満のショートで、検出時のシグナルの平均を比べた差です。", "",
                "| シグナル | 差 |", "| --- | ---: |"]
        out += [f"| {SIGNAL_NAMES.get(k, k)} | {v:+.2f} |" for k, v in sorted(fb.signal_diff.items(), key=lambda x: -x[1])]
        out += [""]
    if fb.duration_finding:
        d = fb.duration_finding
        out += ["## 長さと維持率", "",
                f"- {d['split_seconds']} 秒以下: 平均視聴率 {d['shorter_avg_viewed']}%",
                f"- {d['split_seconds']} 秒超: 平均視聴率 {d['longer_avg_viewed']}%", ""]
    if fb.hook_finding:
        h = fb.hook_finding
        out += ["## 引きの言葉 (冒頭 3 秒) の効果", "",
                f"- 引きの言葉あり: 視聴を継続 {h['with_hook']}%",
                f"- 引きの言葉なし: 視聴を継続 {h['without_hook']}%", ""]
    if fb.recommended:
        out += ["## 推奨設定", "", "`config.feedback.toml` に書き出しました。次回の切り抜きから反映するには `-c` で重ねてください。", "",
                "```toml", feedback_toml(fb).rstrip(), "```", ""]
    return "\n".join(out)


def _pct(v: float | None) -> str:
    return f"{v:.1f}%" if v is not None else "—"


def feedback_toml(fb: FeedbackResult) -> str:
    return overrides_toml(f"YouTube の結果 ({fb.generated_at}) から作った推奨設定", fb.recommended, fb.current)


def write_outputs(fb: FeedbackResult, out_dir: str | Path) -> dict[str, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"report": out_dir / "report.md", "json": out_dir / "feedback.json",
             "config": out_dir / "config.feedback.toml"}
    paths["report"].write_text(report_markdown(fb), encoding="utf-8")
    paths["json"].write_text(json.dumps(asdict(fb), ensure_ascii=False, indent=2), encoding="utf-8")
    paths["config"].write_text(feedback_toml(fb), encoding="utf-8")
    return paths
