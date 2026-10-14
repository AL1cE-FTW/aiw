"""AI (Claude または Gemini) でハイライト候補を再評価し、ショート動画のタイトルを付ける (任意機能)。

- Claude: pip install 'twitch-shorts[llm]' と ANTHROPIC_API_KEY
- Gemini: pip install 'twitch-shorts[gemini]' と GEMINI_API_KEY (Google AI Studio で作成。無料枠あり)
AI が使えない・拒否した・上限に達した場合は、シグナルだけで決めた順位をそのまま使う。
"""

from __future__ import annotations

import json
import logging
import time

from .config import LLM_PROVIDERS, LLMConfig, normalize_provider
from .models import Highlight, normalize_hashtag

log = logging.getLogger(__name__)

DEFAULT_MODELS = {"claude": "claude-opus-5-5", "gemini": "gemini-flash-latest"}

# 場面の種類 (参考: KumoMoments の funny / clutch / wholesome / lore)
CATEGORIES = ["面白い", "スーパープレイ", "ほっこり", "ネタ・名場面", "その他"]

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
                    "hook": {"type": "string", "description": "冒頭 3 秒に出す引きの言葉 (10 文字以内)"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "description": {"type": "string", "description": "投稿時の説明文 (2 文以内)"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                },
                "required": ["id", "score", "title", "hook", "category", "description", "hashtags", "reason", "start", "end"],
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
- hook: 動画の最初の 3 秒に大きく表示する「引きの言葉」。{language}で 10 文字以内。
  ショートは最初の 3 秒でスワイプされるかが決まるため、続きを見たくなる言葉にする
  (例: 「まさかの結末」「これ見て」「1v3の結果…」)。オチそのものは言わない。
- category: 場面の種類。面白い (笑い・ハプニング) / スーパープレイ (クラッチ・神エイム) /
  ほっこり (かわいい・優しい) / ネタ・名場面 (チャンネルの定番ネタ・語り草) / その他 から選ぶ。
- description: YouTube / TikTok に投稿するときの説明文。{language}で 2 文以内、ネタバレしすぎない。
- hashtags: 投稿用ハッシュタグを 3〜5 個 (# 付き)。ゲーム名・チャンネルに関するものを含める。
- reason: 評価理由を 1 文で。
- start / end: 候補区間 (秒) の範囲内で、より良い切り出し位置があれば調整した値。
  長さは {min_dur:.0f}〜{max_dur:.0f} 秒に収めること。調整不要なら元の値をそのまま返す。
すべての候補について、入力と同じ id で結果を返してください。"""


VIDEO_NOTE = """

候補には、その区間の映像 (音声付き) が添付されています。チャットや文字起こしだけでなく、
映像と音声を実際に見て聞いて評価してください。画面の動き・リアクション・ゲームの展開・
表情なども判断材料にし、映像として見栄えのする場面を高く評価してください。"""


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
        # 音声を聞いた AI が選んだ場面なら、何が起きているかのメモ
        "ai_note": h.reason if "ai" in h.signals else "",
    }


def provider_of(cfg: LLMConfig) -> str:
    return normalize_provider(cfg.provider)


def model_of(cfg: LLMConfig) -> str:
    """使うモデル。別の AI 用のモデル名が書かれていたら (provider だけ変えた場合など)、その AI の既定のモデルにする。"""
    provider = provider_of(cfg)
    model = (cfg.model or "").strip()
    other = [p for p in LLM_PROVIDERS if p != provider]
    if not model or any(model.startswith(p) for p in other):
        return DEFAULT_MODELS[provider]
    return model


def rerank_with_llm(
    highlights: list[Highlight],
    cfg: LLMConfig,
    channel: str,
    stream_title: str,
    min_duration: float,
    max_duration: float,
    client=None,
    videos: dict[int, str] | None = None,
) -> list[Highlight]:
    """候補に AI のスコアとタイトルを付けて、良い順に並べ替えて返す。

    videos: 候補の番号 → その区間 (start〜end) を切り出した動画。Gemini のときは映像も見て評価する
    """
    if not highlights:
        return highlights
    user_content = json.dumps(
        {
            "channel": channel,
            "stream_title": stream_title,
            "candidates": [_candidate_payload(h, i) for i, h in enumerate(highlights)],
        },
        ensure_ascii=False,
    )
    system = SYSTEM_PROMPT.format(language=cfg.language, min_dur=min_duration, max_dur=max_duration)
    if provider_of(cfg) == "gemini":
        text = _call_gemini(cfg, system, user_content, client, videos=videos)
    else:
        text = _call_claude(cfg, system, user_content, client)
    if text is None:
        if cfg.require:
            raise RuntimeError("AI が評価結果を返しませんでした")
        return highlights
    return _apply_results(highlights, text, min_duration, max_duration)


# 以前の名前 (互換のため残す)
rerank_with_claude = rerank_with_llm


def _call_claude(cfg: LLMConfig, system: str, user_content: str, client=None) -> str | None:
    if client is None:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - 任意依存
            raise RuntimeError("Claude を使うには anthropic が必要です: pip install 'twitch-shorts[llm]'") from e
        client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=model_of(cfg),
        max_tokens=16000,
        # ポリシー判定で拒否された場合にサーバー側で別モデルへ自動フォールバックさせる
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=system,
        messages=[{"role": "user", "content": user_content}],
        output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
    )
    if response.stop_reason == "refusal":
        log.warning("AI が評価を拒否したため、シグナルのみの順位を使います")
        return None
    if response.stop_reason == "max_tokens":
        log.warning("AI の出力が途中で切れたため、シグナルのみの順位を使います")
        return None
    return next((b.text for b in response.content if b.type == "text"), "")


def _gemini_client(timeout_ms: int):
    try:
        from google import genai
    except ImportError as e:  # pragma: no cover - 任意依存
        raise RuntimeError("Gemini を使うには google-genai が必要です: pip install 'twitch-shorts[gemini]'") from e
    # GEMINI_API_KEY を読む。返事が無いときに固まらないよう時間で諦め、混雑 (429 / 503) は間を空けて 5 回まで試す
    return genai.Client(http_options={
        "timeout": timeout_ms,
        "retry_options": {"attempts": 5, "initial_delay": 10, "max_delay": 120,
                          "http_status_codes": [429, 500, 503, 504]},
    })


def _upload(client, path: str):
    """Gemini にファイルを送り、使える状態 (ACTIVE) になるまで待つ。"""
    f = client.files.upload(file=path)
    for _ in range(120):  # 最大 10 分
        state = getattr(f.state, "name", str(f.state))
        if state == "ACTIVE":
            return f
        if state == "FAILED":
            raise RuntimeError(f"Gemini がファイルを読み込めませんでした: {path}")
        time.sleep(5)
        f = client.files.get(name=f.name)
    raise RuntimeError(f"Gemini のファイルの準備が終わりませんでした: {path}")


def _delete(client, files) -> None:
    for f in files:
        try:
            client.files.delete(name=f.name)
        except Exception:  # 消せなくても 48 時間で自動的に消える
            pass


def _call_gemini(cfg: LLMConfig, system: str, user_content: str, client=None,
                 videos: dict[int, str] | None = None) -> str | None:
    client = client or _gemini_client(900_000 if videos else 300_000)
    uploaded = []
    try:
        contents: str | list = user_content
        if videos:
            contents = [user_content]
            for idx, path in sorted(videos.items()):
                uploaded.append(_upload(client, path))
                contents += [f"候補 id={idx} の映像 (映像の 0 秒 = この候補の start)", uploaded[-1]]
        config = {
            "system_instruction": system + (VIDEO_NOTE if videos else ""),
            "response_mime_type": "application/json",
            "response_json_schema": RESULT_SCHEMA,
            # 出力の上限はモデルごとに違うので指定しない (モデルの上限まで使う)
        }
        if videos:
            config["media_resolution"] = "MEDIA_RESOLUTION_LOW"  # 映像のトークンを 1/4 に (縦型の判断には十分)
        response = client.models.generate_content(model=model_of(cfg), contents=contents, config=config)
    finally:
        _delete(client, uploaded)
    candidate = (getattr(response, "candidates", None) or [None])[0]
    reason = getattr(getattr(candidate, "finish_reason", None), "name", None) or str(
        getattr(candidate, "finish_reason", "") or "")
    if reason.endswith("MAX_TOKENS"):
        log.warning("AI の出力が途中で切れたため、シグナルのみの順位を使います")
        return None
    text = getattr(response, "text", None)
    if not text:
        log.warning("AI が結果を返さなかったため (%s)、シグナルのみの順位を使います", reason or "理由不明")
        return None
    return text


SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "moments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "開始時刻 H:MM:SS"},
                    "end": {"type": "string", "description": "終了時刻 H:MM:SS"},
                    "score": {"type": "number", "description": "0〜10。ショート動画としての面白さ"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "description": {"type": "string"},
                    "hashtags": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string", "description": "何が起きているか (発言・反応) を 1〜2 文で"},
                },
                "required": ["start", "end", "score", "title", "hook", "category", "description", "hashtags",
                             "reason"],
            },
        }
    },
    "required": ["moments"],
}

SCAN_PROMPT = """あなたは Twitch 配信の切り抜き編集者です。
添付は配信「{stream_title}」(チャンネル: {channel}、長さ {length}) の音声をまるごと録ったものです。
最初から最後まで聞いて、YouTube Shorts / TikTok 向けの縦型ショート動画にすると面白い場面を
最大 {count} 個選んでください。

- 選ぶ場面: 大笑い・叫び・ツッコミ・ボケ・ハプニング・名言・スーパープレイへの興奮・オチのある会話など、
  前後の文脈なしで単体で見ても分かって面白いもの。雑談の途中や BGM だけの部分は選ばない。
- start / end: 音声の先頭からの時刻を H:MM:SS で。場面の前フリから始めて、オチ・反応まで含める。
  長さは {min_dur:.0f}〜{max_dur:.0f} 秒。場面どうしは重ならないようにする。
- score: 0〜10。面白さ・分かりやすさが高いものほど高く。
- title: {language}で 25 文字以内。釣りすぎず、内容が伝わる切り抜きタイトル。
- hook: 動画の最初の 3 秒に大きく表示する「引きの言葉」。{language}で 10 文字以内。オチそのものは言わない。
- category: 面白い / スーパープレイ / ほっこり / ネタ・名場面 / その他 から選ぶ。
- description: 投稿用の説明文。{language}で 2 文以内、ネタバレしすぎない。
- hashtags: 投稿用ハッシュタグを 3〜5 個 (# 付き)。
- reason: その場面で何が起きているか (発言・反応) を 1〜2 文で。"""


def _parse_ts(value) -> float:
    """"1:02:03" / "02:03" / 秒数 を秒にする。"""
    if isinstance(value, (int, float)):
        return float(value)
    secs = 0.0
    for part in str(value).strip().split(":"):
        secs = secs * 60 + float(part)
    return secs


def _hms(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}"


def scan_audio_with_gemini(
    audio_path: str,
    cfg: LLMConfig,
    channel: str,
    stream_title: str,
    duration: float,
    min_duration: float,
    max_duration: float,
    count: int,
    client=None,
) -> list[Highlight]:
    """配信の音声をまるごと Gemini に聞かせて、面白い場面を選ばせる。

    ponytail: 映像は送らない (2 時間の配信だと Gemini の入力上限を超える)。音声なら 2 時間で約 23 万トークン。
    """
    client = client or _gemini_client(900_000)  # 長い音声を聞くので 15 分まで待つ
    files = []
    try:
        files.append(uploaded := _upload(client, audio_path))
        prompt = SCAN_PROMPT.format(
            stream_title=stream_title or "(不明)", channel=channel, length=_hms(duration), count=count,
            min_dur=min_duration, max_dur=max_duration, language=cfg.language,
        )
        response = client.models.generate_content(
            model=model_of(cfg),
            contents=[uploaded, prompt],
            config={"response_mime_type": "application/json", "response_json_schema": SCAN_SCHEMA},
        )
    finally:
        _delete(client, files)
    try:
        moments = json.loads(response.text or "")["moments"]
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning("Gemini の場面選びの結果を解釈できませんでした: %s", (response.text or "")[:200])
        return []

    found: list[Highlight] = []
    for m in moments:
        try:
            start, end = _parse_ts(m["start"]), _parse_ts(m["end"])
            score = max(0.0, min(10.0, float(m.get("score", 0))))
        except (KeyError, TypeError, ValueError):
            continue
        start = max(0.0, min(start, duration - min_duration))
        end = min(duration, max(end, start + min_duration), start + max_duration)
        if end <= start or any(start < h.end and h.start < end for h in found):
            continue
        tags = m.get("hashtags") or []
        if isinstance(tags, str):
            tags = tags.split()
        found.append(Highlight(
            start=round(start, 2), end=round(end, 2), peak=round((start + end) / 2, 2),
            # ponytail: AI の 0〜10 をそのままシグナルの点数として使う (シグナルの合成スコアと大体同じ範囲)
            score=score, signals={"ai": score},
            title=str(m.get("title") or "").strip(), hook=str(m.get("hook") or "").strip(),
            category=str(m.get("category") or "").strip(),
            description=str(m.get("description") or "").strip(),
            hashtags=[t for t in (normalize_hashtag(x) for x in tags if isinstance(x, str)) if t],
            reason=str(m.get("reason") or "").strip(),
        ))
    return found


def _apply_results(highlights: list[Highlight], text: str, min_duration: float,
                   max_duration: float) -> list[Highlight]:
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
        h.hook = str(r.get("hook") or "").strip()
        h.category = str(r.get("category") or "").strip()
        h.description = str(r.get("description") or "").strip()
        tags = r.get("hashtags") or []
        if isinstance(tags, str):  # 1 つの文字列で返ってきた場合 ("#apex #clutch")
            tags = tags.split()
        h.hashtags = [t for t in (normalize_hashtag(x) for x in tags if isinstance(x, str)) if t]
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
