"""チャット・音量・既存クリップのシグナルから「盛り上がった場面」を検出する。

考え方:
  1. 1 秒刻みの時系列を作る
     - chat:     1 秒あたりの発言人数 (同一ユーザーの連投は 1 と数える)
     - keywords: 「草」「pog」「クリップ」等の盛り上がりワードの重み合計
     - audio:    配信音声の音量 (dB)
     - clips:    視聴者が作った既存クリップの範囲 (再生数で重み付け)
  2. chat / keywords は反応の遅れ (chat_delay) の分だけ前にずらす
  3. 各シグナルを「その時間帯の平常値」からのずれ (ロバスト z 値) に正規化して重み付き合計
  4. スコアの高い順にピークを取り、前後に余白を付けて 15〜59 秒の区間にする
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np

from .config import DetectConfig
from .models import ChatMessage, ClipRef, Highlight, TranscriptSegment

BASELINE_WINDOW = 300  # 平常値を計算する窓(秒)。配信が進むとチャットが増える傾向を吸収する


def moving_average(x: np.ndarray, window: float) -> np.ndarray:
    w = max(1, int(round(window)))
    if w <= 1 or len(x) == 0:
        return x.astype(np.float64)
    kernel = np.ones(w) / w
    pad_l, pad_r = w // 2, w - 1 - w // 2
    padded = np.pad(x.astype(np.float64), (pad_l, pad_r), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def local_zscore(x: np.ndarray, baseline_window: int = BASELINE_WINDOW) -> np.ndarray:
    """長い窓の移動平均を平常値とみなし、そこからのずれをロバストに標準化する。"""
    if len(x) == 0:
        return x.astype(np.float64)
    baseline = moving_average(x, min(baseline_window, max(1, len(x))))
    dev = x - baseline
    mad = np.median(np.abs(dev - np.median(dev)))
    scale = 1.4826 * mad
    if scale < 1e-9:
        scale = float(np.std(dev))
    if scale < 1e-9:
        return np.zeros_like(dev)
    return dev / scale


def keyword_weight(text: str, keywords: dict[str, float]) -> float:
    t = text.lower()
    score = sum(w for k, w in keywords.items() if k in t)
    return min(score, 3.0)  # 1 メッセージでの稼ぎすぎを防ぐ


def build_signals(
    duration: float,
    chat: list[ChatMessage],
    cfg: DetectConfig,
    loudness: np.ndarray | None = None,
    clips: list[ClipRef] | None = None,
) -> dict[str, np.ndarray]:
    n = max(1, int(math.ceil(duration)))
    chat_users = np.zeros(n)
    kw = np.zeros(n)
    seen_per_sec: dict[int, set[str]] = {}
    for m in chat:
        i = int(m.offset - cfg.chat_delay)
        if i < 0 or i >= n:
            continue
        users = seen_per_sec.setdefault(i, set())
        if m.user not in users:
            users.add(m.user)
            chat_users[i] += 1
        kw[i] += keyword_weight(m.text, cfg.keywords) + 0.3 * min(len(m.emotes), 3)

    signals = {"chat": chat_users, "keywords": kw}

    if loudness is not None and len(loudness):
        audio = np.full(n, float(np.median(loudness)))
        k = min(n, len(loudness))
        audio[:k] = loudness[:k]
        signals["audio"] = audio

    if clips:
        clip_sig = np.zeros(n)
        for c in clips:
            a = max(0, int(c.offset))
            b = min(n, int(math.ceil(c.offset + max(c.duration, 1))))
            clip_sig[a:b] += 1.0 + math.log1p(max(c.views, 0))
        signals["clips"] = clip_sig
    return signals


def combine_signals(signals: dict[str, np.ndarray], cfg: DetectConfig) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """各シグナルを正規化して重み付き合計したスコア系列と、正規化後の各系列を返す。"""
    weights = {
        "chat": cfg.weight_chat,
        "keywords": cfg.weight_keywords,
        "audio": cfg.weight_audio,
        "clips": cfg.weight_clips,
    }
    normed: dict[str, np.ndarray] = {}
    n = len(next(iter(signals.values())))
    total = np.zeros(n)
    for name, raw in signals.items():
        if name == "clips":
            # クリップは疎なので z 値ではなく最大値で 0〜4 に正規化する
            mx = raw.max()
            z = raw / mx * 4.0 if mx > 0 else raw
        elif name == "audio":
            # 音量は短い窓で均してから z 値化 (BGM や瞬間的なノイズに引っ張られすぎないように)
            z = local_zscore(moving_average(raw, 3))
        else:
            z = local_zscore(moving_average(raw, cfg.smooth_seconds))
        z = np.clip(z, -2.0, 6.0)
        normed[name] = z
        total += weights.get(name, 0.0) * z
    used = sum(weights.get(k, 0.0) for k in signals) or 1.0
    # シグナルの数によらず同じ閾値が使えるように、重みの合計で割って平均的なスケールに戻す
    score = moving_average(total / used * len(signals) ** 0.5, max(1.0, cfg.smooth_seconds / 2))
    return score, normed


def _fit_window(peak: int, left: int, right: int, n: int, cfg: DetectConfig) -> tuple[float, float]:
    start = min(left, peak - cfg.pre_roll)
    end = max(right, peak + cfg.post_roll)
    if end - start > cfg.max_duration:
        # 盛り上がりが長すぎる場合はピーク周辺を pre:post の比率で切り出す
        ratio = cfg.pre_roll / max(cfg.pre_roll + cfg.post_roll, 1e-6)
        start = peak - cfg.max_duration * ratio
        end = start + cfg.max_duration
    if end - start < cfg.min_duration:
        extra = cfg.min_duration - (end - start)
        start -= extra * 0.6
        end += extra * 0.4
    # 動画の範囲内に収める (長さは極力保つ)
    if start < 0:
        end = min(n, end - start)
        start = 0.0
    if end > n:
        start = max(0.0, start - (end - n))
        end = float(n)
    return float(start), float(end)


def detect_highlights(
    duration: float,
    chat: list[ChatMessage],
    cfg: DetectConfig,
    loudness: np.ndarray | None = None,
    clips: list[ClipRef] | None = None,
    top_n: int | None = None,
) -> tuple[list[Highlight], np.ndarray]:
    """ハイライト候補をスコア順に返す。2 番目の戻り値はスコア系列 (可視化・デバッグ用)。"""
    signals = build_signals(duration, chat, cfg, loudness, clips)
    score, normed = combine_signals(signals, cfg)
    n = len(score)
    limit = top_n if top_n is not None else cfg.top_n
    masked = score.copy()
    best = float(score.max()) if n else 0.0
    floor = max(cfg.min_score, best * cfg.min_relative_score)
    results: list[Highlight] = []
    while len(results) < limit:
        peak = int(np.argmax(masked))
        peak_score = float(masked[peak])
        if not np.isfinite(peak_score) or peak_score < floor:
            break
        # ピークから、スコアが十分高い範囲を左右に広げる
        thresh = max(cfg.min_score * 0.5, peak_score * 0.4)
        left = peak
        while left > 0 and score[left - 1] >= thresh and peak - left < cfg.max_duration:
            left -= 1
        right = peak
        while right < n - 1 and score[right + 1] >= thresh and right - peak < cfg.max_duration:
            right += 1
        start, end = _fit_window(peak, left, right + 1, n, cfg)
        a = max(0, int(start - cfg.min_gap))
        b = min(n, int(math.ceil(end + cfg.min_gap)))
        masked[a:b] = -np.inf
        results.append(
            Highlight(
                start=round(start, 2),
                end=round(end, 2),
                peak=float(peak),
                score=round(peak_score, 3),
                signals={k: round(float(v[peak]), 3) for k, v in normed.items()},
                chat_sample=sample_chat(chat, start, end + cfg.chat_delay),
            )
        )
    return results, score


def sample_chat(chat: list[ChatMessage], start: float, end: float, limit: int = 40) -> list[str]:
    """区間内のチャットから代表的なものを選ぶ (頻出メッセージ優先 + 時系列サンプル)。"""
    texts = [m.text.strip() for m in chat if start <= m.offset < end and m.text.strip()]
    if not texts:
        return []
    counts = Counter(texts)
    common = [t for t, _ in counts.most_common(limit // 2)]
    common_set = set(common)
    rest = [t for t in texts if t not in common_set]
    room = limit - len(common)
    if rest and room > 0:
        rest = rest[:: max(1, len(rest) // room)][:room]
    return [f"{t} (x{counts[t]})" if counts[t] > 1 else t for t in common] + rest


def snap_to_transcript(h: Highlight, segments: list[TranscriptSegment], cfg: DetectConfig,
                       max_shift: float = 4.0) -> Highlight:
    """発話の途中で始まったり終わったりしないように、区間の端を文の切れ目に合わせる。"""
    if not segments:
        return h
    starts = [s.start for s in segments if h.start - max_shift <= s.start <= h.start + 1.0]
    ends = [s.end for s in segments if h.end - 1.0 <= s.end <= h.end + max_shift]
    # 開始点の途中にかかっている発話があれば、その頭まで戻す
    covering = [s for s in segments if s.start < h.start < s.end and h.start - s.start <= max_shift]
    new_start = covering[0].start if covering else (max(starts) if starts else h.start)
    covering_end = [s for s in segments if s.start < h.end < s.end and s.end - h.end <= max_shift]
    new_end = covering_end[0].end if covering_end else (min(ends, key=lambda e: abs(e - h.end)) if ends else h.end)
    if new_end - new_start > cfg.max_duration:
        new_end = new_start + cfg.max_duration
    if new_end - new_start >= cfg.min_duration * 0.8:
        h.start, h.end = round(max(0.0, new_start), 2), round(new_end, 2)
    return h
