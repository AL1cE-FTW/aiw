"""Claude でハイライト候補を再評価し、ショート動画のタイトルを付ける (任意機能)。

pip install 'twitch-shorts[llm]' と ANTHROPIC_API_KEY の設定が必要。
LLM が使えない・拒否した場合は、シグナルだけで決めた順位をそのまま使う。
"""

from __future__ import annotations

import json
import logging

from .config import LLMConfig
from .models import Highlight

log = logging.getLogger(__name__)

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "number", "description": "0〜10。ショート動画としての面白さ"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                },
                "required": ["id", "score", "title", "reason", "start", "end"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """あなたは Twitch 配信の切り抜き編集者です。
自動検出された「盛り上がり候補」それぞれについて、チャットの反応と配信者の発話から
何が起きた場面かを推測し、YouTube Shorts / TikTok 向けの縦型ショート動画として
どれくらい見応えがあるかを評価してください。

- score: 0〜10。単発で見て状況が分かり、オチ・驚き・笑い・スーパープレイがあるものを高く。
  雑談の途中やチャットの挨拶ラッシュなど、文脈なしでは面白くない場面は低く。
- title: {language}で 25 文字以内。釣りすぎず、内容が伝わる切り抜きタイトル。
- reason: 評価理由を 1 文で。
- start / end: 候補区間 (秒) の範囲内で、より良い切り出し位置があれば調整した値。
  長さは {min_dur:.0f}〜{max_dur:.0f} 秒に収めること。調整不要なら元の値をそのまま返す。
すべての候補について、入力と同じ id で結果を返してください。"""


def _candidate_payload(h: Highlight, idx: int) -> dict:
    return {
        "id": idx,
        "start": h.start,
        "end": h.end,
        "peak": h.peak,
        "signal_score": h.score,
        "signals": h.signals,
        "chat": h.chat_sample[:40],
        "transcript": h.transcript[:2000],
    }


def rerank_with_claude(
    highlights: list[Highlight],
    cfg: LLMConfig,
    channel: str,
    stream_title: str,
    min_duration: float,
    max_duration: float,
    client=None,
) -> list[Highlight]:
    """候補に LLM のスコアとタイトルを付けて、良い順に並べ替えて返す。"""
    if not highlights:
        return highlights
    if client is None:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - 任意依存
            raise RuntimeError("LLM 機能には anthropic が必要です: pip install 'twitch-shorts[llm]'") from e
        client = anthropic.Anthropic()

    user_content = json.dumps(
        {
            "channel": channel,
            "stream_title": stream_title,
            "candidates": [_candidate_payload(h, i) for i, h in enumerate(highlights)],
        },
        ensure_ascii=False,
    )
    response = client.beta.messages.create(
        model=cfg.model,
        max_tokens=16000,
        # ポリシー判定で拒否された場合にサーバー側で別モデルへ自動フォールバックさせる
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT.format(language=cfg.language, min_dur=min_duration, max_dur=max_duration),
        messages=[{"role": "user", "content": user_content}],
        output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        log.warning("LLM が評価を拒否したため、シグナルのみの順位を使います")
        return highlights
    if response.stop_reason == "max_tokens":
        log.warning("LLM の出力が途中で切れたため、シグナルのみの順位を使います")
        return highlights
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        results = json.loads(text)["candidates"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("LLM の出力を解釈できませんでした: %s", text[:200])
        return highlights

    by_id = {r["id"]: r for r in results if isinstance(r.get("id"), int)}
    max_signal = max(h.score for h in highlights) or 1.0
    ranked: list[tuple[float, Highlight]] = []
    for i, h in enumerate(highlights):
        r = by_id.get(i)
        if r is None:
            ranked.append((h.score / max_signal * 0.5, h))
            continue
        h.title = str(r.get("title") or "").strip()
        h.reason = str(r.get("reason") or "").strip()
        start, end = float(r.get("start", h.start)), float(r.get("end", h.end))
        # LLM の提案は元の候補区間の内側かつ長さ制約を満たす場合だけ採用する
        if h.start - 1 <= start < end <= h.end + 1 and min_duration <= end - start <= max_duration:
            h.start, h.end = round(start, 2), round(end, 2)
        llm_score = max(0.0, min(10.0, float(r.get("score", 0)))) / 10.0
        # LLM の評価を主、チャット等のシグナルを従として合成する
        ranked.append((0.7 * llm_score + 0.3 * h.score / max_signal, h))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in ranked]
