"""検出 → (文字起こし) → (LLM 評価) → レンダリング の一連の処理。"""

from __future__ import annotations

import json
import logging
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from . import audio as audio_mod
from .config import Config
from .detector import detect_highlights, snap_to_transcript
from .models import ChatMessage, ClipRef, Highlight, TranscriptSegment, ViewerSample, normalize_hashtag
from .render import render_highlight

log = logging.getLogger(__name__)

# 一覧ページに載せる次点の候補の数
WEB_EXTRA_CANDIDATES = 10


class MediaSource(Protocol):
    def audio_path(self) -> str | None: ...

    def video_for(self, start: float, end: float) -> tuple[str, float]:
        """[start, end) を含む動画ファイルと、そのファイル先頭のソース上の秒数を返す。"""
        ...


@dataclass
class LocalSource:
    path: str

    def audio_path(self) -> str | None:
        return self.path

    def video_for(self, start: float, end: float) -> tuple[str, float]:
        return self.path, 0.0


@dataclass
class VodSource:
    """Twitch VOD。解析用に音声だけを落とし、動画は必要な区間だけダウンロードする。"""

    url: str
    work_dir: Path
    quality: str = "best"
    full_download: bool = False
    _audio: str | None = None

    def audio_path(self) -> str | None:
        from .download import download_audio

        if self._audio is None:
            try:
                self._audio = str(download_audio(self.url, self.work_dir))
            except Exception as e:  # 音声が取れなくてもチャットだけで検出は続ける
                log.warning("音声のダウンロードに失敗しました (チャットのみで検出します): %s", e)
                self._audio = ""
        return self._audio or None

    def video_for(self, start: float, end: float) -> tuple[str, float]:
        from .download import download_section, download_video

        if self.full_download:
            return str(download_video(self.url, self.work_dir, self.quality)), 0.0
        # 前後に余裕を持たせて落とし、レンダリング時に正確に切る
        a = max(0.0, start - 3)
        b = end + 3
        out = self.work_dir / "sections" / f"{int(a)}-{int(b)}.mp4"
        return str(download_section(self.url, a, b, out, self.quality)), a


@dataclass
class RunResult:
    highlights: list[Highlight]
    run_dir: Path
    score: np.ndarray = field(default_factory=lambda: np.zeros(0))


class _KeepUnknown(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _template_vars(h: Highlight, index: int, channel: str) -> dict:
    top = Counter(s.rsplit(" (x", 1)[0] for s in h.chat_sample).most_common(1)
    return {"channel": channel, "index": index, "date": datetime.now().strftime("%Y-%m-%d"),
            "top_chat": top[0][0] if top else ""}


def fill_template(template: str, values: dict) -> str:
    """{channel} などを埋める。知らない変数や {} の書き間違いがあってもエラーにしない。"""
    try:
        return template.format_map(_KeepUnknown(values)).strip()
    except (ValueError, IndexError, AttributeError, TypeError, KeyError):
        log.warning("テンプレートを解釈できないため、そのまま使います: %s", template)
        return template.strip()


def default_title(cfg: Config, h: Highlight, index: int, channel: str) -> str:
    return fill_template(cfg.render.title_template, _template_vars(h, index, channel))


def process(
    cfg: Config,
    source: MediaSource,
    chat: list[ChatMessage],
    run_dir: str | Path,
    duration: float | None = None,
    clips: list[ClipRef] | None = None,
    channel: str = "",
    stream_title: str = "",
    exclude: list[Highlight] | None = None,
    available_until: float | None = None,
    available_from: float | None = None,
    dry_run: bool = False,
    llm_client=None,
    viewers: list[ViewerSample] | None = None,
    clip_vod: tuple[str, float] | None = None,
    clip_owner: str | None = None,
    publish: bool = True,
) -> RunResult:
    """ハイライトを検出し、ショート動画を ``run_dir`` に書き出す。

    exclude:          既に書き出したハイライト (ライブの逐次処理で重複させないため)
    available_until:  これより後ろにかかる区間は採用しない (録画中のファイル用)
    available_from:   これより前にかかる区間は採用しない
    clip_vod:         (VOD の ID, ハイライトの時刻を VOD 上の時刻にするために足す秒数)。
                      clips.enabled のとき、この VOD から Twitch の公式クリップを作る
    clip_owner:       VOD の持ち主のチャンネル名 (省略時は channel)。クリップを作れるかの判定に使う
    publish:          False なら投稿予定表に載せない (配信中のプレビューなど)
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    exclude = exclude or []

    apath = source.audio_path()
    loudness = None
    if apath:
        log.info("音声を解析中: %s", apath)
        loudness = audio_mod.loudness_per_second(apath)
    if duration is None:
        if loudness is not None and len(loudness):
            duration = float(len(loudness))
        elif chat:
            duration = chat[-1].offset + 1
        else:
            raise ValueError("動画の長さが分かりません")
    if not chat and loudness is None and not clips and not viewers:
        raise ValueError("チャット・音声・クリップのいずれも無いため検出できません")

    n_target = cfg.detect.top_n
    n_pool = n_target * (cfg.llm.candidate_factor if cfg.llm.enabled else 1)
    # 一覧ページ (worker/) に送るときは、次点の候補も載せられるよう多めに検出する
    want_web = bool(cfg.web.url and cfg.web.upload_token and clip_vod and publish and not dry_run)
    if cfg.web.url and not cfg.web.upload_token and clip_vod and publish and not dry_run:
        log.warning("[web] url はありますが upload_token が無いため、切り抜き候補ページには送りません")
    n_extra = WEB_EXTRA_CANDIDATES if want_web else 0
    # 画面が止まっている場面を飛ばしたときに埋める予備の候補
    n_spare = n_target if cfg.detect.max_frozen < 1 and not dry_run else 0
    n_candidates = n_pool + n_spare + n_extra + len(exclude) * 2
    candidates, score = detect_highlights(
        duration, chat, cfg.detect, loudness, clips, top_n=n_candidates, viewers=viewers
    )
    candidates = [
        h for h in candidates
        if not any(h.overlaps(e, margin=5) for e in exclude)
        and (available_until is None or h.end <= available_until)
        and (available_from is None or h.start >= available_from)
    ]
    spares = candidates[n_pool:n_pool + n_spare]
    extras = candidates[n_pool + n_spare:n_pool + n_spare + n_extra]  # ショートの対象外。一覧ページにだけ載せる
    candidates = candidates[:n_pool]
    log.info("候補 %d 件を検出", len(candidates))

    from .llm import provider_of

    # 配信中のプレビュー (publish=False) では、配信の音声をまるごと聞かせる処理は重いので配信後の正式版だけで行う
    if cfg.llm.enabled and cfg.llm.scan_audio and provider_of(cfg.llm) == "gemini" and apath and publish:
        from .llm import model_of, scan_audio_with_gemini

        try:
            log.info("AI (gemini) が配信の音声を聞いて面白い場面を探しています (%s)", model_of(cfg.llm))
            with tempfile.TemporaryDirectory() as tmp:
                speech = str(Path(tmp) / "speech.mp3")
                audio_mod.speech_mp3(apath, speech)
                found = scan_audio_with_gemini(
                    speech, cfg.llm, channel, stream_title, duration,
                    cfg.detect.min_duration, cfg.detect.max_duration, n_pool, client=llm_client,
                )
            found = [
                h for h in found
                if not any(h.overlaps(e, margin=5) for e in exclude)
                and (available_until is None or h.end <= available_until)
                and (available_from is None or h.start >= available_from)
                and h.start >= cfg.detect.skip_start
                and (cfg.detect.skip_end <= 0 or h.end <= duration - cfg.detect.skip_end)
            ]
            for h in found:
                h.chat_sample = [m.text for m in chat if h.start <= m.offset <= h.end + cfg.detect.chat_delay][:40]
            log.info("AI が音声から %d 件の場面を選びました", len(found))
            # 重なる場面は AI が選んだ方 (区間・タイトル付き) を残す
            # AI の点数 (0〜10) とシグナルの点数は大体同じ範囲なので、まとめて点数順に並べる
            # (後の AI の評価が失敗したときも、良い候補から選ばれるように)
            candidates = sorted(found + [h for h in candidates if not any(h.overlaps(f) for f in found)],
                                key=lambda h: h.score, reverse=True)
        except Exception as e:
            if cfg.llm.require:
                raise
            log.warning("AI による音声からの場面選びに失敗したため、シグナルの候補だけを使います: %s", e)

    transcripts: dict[int, list[TranscriptSegment]] = {}
    transcriber = None

    def transcribe(h: Highlight) -> None:
        nonlocal transcriber
        if transcriber is None:
            from .transcribe import Transcriber

            transcriber = Transcriber(cfg.transcribe)
        segs = transcriber.transcribe_range(apath, max(0.0, h.start - 5), h.end + 5)
        transcripts[id(h)] = segs
        snap_to_transcript(h, segs, cfg.detect)
        h.transcript = " ".join(s.text for s in segs if s.end > h.start and s.start < h.end)

    if cfg.transcribe.enabled and candidates and apath:
        for i, h in enumerate(candidates):
            log.info("文字起こし中 (%d/%d)", i + 1, len(candidates))
            transcribe(h)

    if cfg.llm.enabled and candidates:
        from .llm import model_of, rerank_with_llm

        with tempfile.TemporaryDirectory() as tmp:
            videos: dict[int, str] = {}
            if cfg.llm.watch_video and provider_of(cfg.llm) == "gemini" and not dry_run:
                for i, h in enumerate(candidates):
                    log.info("AI に見せる映像を用意中 (%d/%d)", i + 1, len(candidates))
                    try:
                        video, offset = source.video_for(h.start, h.end)
                        preview = Path(tmp) / f"{i}.mp4"
                        audio_mod.preview_clip(video, h.start - offset, h.duration, str(preview))
                        videos[i] = str(preview)
                    except Exception as e:  # 映像が用意できない候補は文字情報だけで評価する
                        log.warning("候補 %d の映像を用意できませんでした: %s", i + 1, e)
            try:
                log.info("AI (%s) で候補を評価中 (%s%s)", provider_of(cfg.llm), model_of(cfg.llm),
                         f"、映像 {len(videos)} 本" if videos else "")
                candidates = rerank_with_llm(
                    candidates, cfg.llm, channel, stream_title,
                    cfg.detect.min_duration, cfg.detect.max_duration, client=llm_client, videos=videos or None,
                )
            except Exception as e:
                if cfg.llm.require:
                    raise
                log.warning("LLM 評価に失敗したため、シグナルのみの順位を使います: %s", e)

    frozen_ids: set[int] = set()
    videos_of: dict[int, tuple[str, float]] = {}  # 調べるときに用意した動画を書き出しでも使う
    if dry_run or cfg.detect.max_frozen >= 1:
        selected = candidates[:n_target]
    else:
        # 画面がほとんど動いていない場面 (映っていない画面・ロード画面など) は飛ばし、次点の候補で埋める
        selected = []
        for h in candidates + spares:
            if len(selected) >= n_target:
                break
            if any(h.overlaps(s) for s in selected):
                continue
            try:
                video, offset = videos_of[id(h)] = source.video_for(h.start, h.end)
                frozen = audio_mod.frozen_ratio(video, h.start - offset, h.duration)
            except Exception as e:  # 調べられない場面は書き出しを試す (失敗すれば書き出しの所で飛ばす)
                log.warning("画面が動いているか調べられませんでした (%s〜): %s", _hms(h.start, ":"), e)
                frozen = 0.0
            if frozen >= cfg.detect.max_frozen:
                log.info("画面がほとんど動いていないので次点の候補にします (%s〜%s, %.0f%%)",
                         _hms(h.start, ":"), _hms(h.end, ":"), frozen * 100)
                frozen_ids.add(id(h))
                continue
            if cfg.transcribe.enabled and apath and id(h) not in transcripts:  # 予備の候補はここで文字起こしする
                try:
                    bounds = (h.start, h.end)
                    transcribe(h)
                    if (h.start, h.end) != bounds:  # 区間が変わったら、書き出しのときに動画を用意し直す
                        videos_of.pop(id(h), None)
                except Exception as e:
                    log.warning("文字起こしに失敗しました (字幕なしで書き出します): %s", e)
            selected.append(h)
    # 出力ファイルは時系列順に番号を振る
    selected.sort(key=lambda h: h.start)
    start_index = len(exclude) + 1
    for i, h in enumerate(selected, start=start_index):
        if not h.title:
            h.title = default_title(cfg, h, i, channel)
        _fill_post_text(cfg, h, channel, i)
        if clip_vod:
            h.vod_url = vod_timestamp_url(clip_vod[0], h.start + clip_vod[1])
        if dry_run:
            continue
        out = run_dir / f"short_{i:02d}_{_hms(h.start)}.mp4"
        log.info("書き出し中: %s (%s〜%s) %s", out.name, _hms(h.start, ":"), _hms(h.end, ":"), h.title)
        try:
            video, offset = videos_of.get(id(h)) or source.video_for(h.start, h.end)
            render_highlight(video, str(out), h, cfg.render, transcripts.get(id(h)), source_offset=offset,
                             max_total=cfg.detect.max_duration)
        except Exception as e:  # 1 本の失敗 (区間のダウンロード失敗など) で残りを止めない
            log.warning("書き出しに失敗したため、この 1 本は飛ばします (%s): %s", out.name, e)
            continue
        h.output_path = str(out)

    # Twitch クリップは書き出せたショートの場面だけ作る (dry-run では作らない)
    rendered = [h for h in selected if h.output_path]
    if cfg.clips.enabled and clip_vod and rendered and not dry_run:
        _make_twitch_clips(cfg, rendered, clip_vod, already=sum(1 for e in exclude if e.twitch_clip),
                           owner=channel if clip_owner is None else clip_owner)
    _write_report(run_dir, selected + exclude, channel, stream_title, duration)
    if want_web:
        # 配信後の一覧ページ (worker/) に、ショートにした場面と次点の候補を送る
        from .web_upload import build_payload, run_id_for, upload_run

        chosen = {id(h) for h in selected} | frozen_ids
        listed = selected + [h for h in candidates + spares + extras if id(h) not in chosen]
        for h in listed:
            if not h.title:
                h.title = f"{_hms(h.start, ':')} の場面"
        upload_run(cfg, build_payload(run_id_for(clip_vod[0], run_dir.name), channel, stream_title, clip_vod[0],
                                      clip_vod[1], listed, selected))
    try:
        from .review import write_review_page

        page = write_review_page(run_dir, selected + exclude, score, duration, channel, stream_title)
        log.info("確認ページ: %s", page)
    except OSError as e:
        log.warning("確認ページを作れませんでした: %s", e)
    if cfg.publish.enabled and publish and not dry_run:
        from .schedule import add_to_schedule

        try:
            for e in add_to_schedule(cfg, selected, channel):
                log.info("投稿予定: %s  %s", e["publish_at"][:16].replace("T", " "), e["title"])
        except (ValueError, KeyError, OSError) as e:  # 設定ミスで書き出し済みの結果を失わないように
            log.warning("投稿予定表に追加できませんでした ([publish] の設定を確認してください): %s", e)
    try:
        from .review import write_portal

        log.info("一覧ページ: %s", write_portal(cfg))
    except (ValueError, KeyError, OSError) as e:
        log.warning("一覧ページを作れませんでした: %s", e)
    return RunResult(selected, run_dir, score)


def _fill_post_text(cfg: Config, h: Highlight, channel: str, index: int = 1) -> None:
    """投稿用の説明文・ハッシュタグを補う (AI が付けていればそれを優先)。"""
    if not h.description:
        h.description = fill_template(cfg.publish.description_template,
                                      _template_vars(h, index, channel or "配信"))
    tags: list[str] = []
    seen: set[str] = set()
    for t in map(normalize_hashtag, [*h.hashtags, *cfg.publish.hashtags]):
        if t and t.lower() not in seen:
            seen.add(t.lower())
            tags.append(t)
    h.hashtags = tags


def vod_timestamp_url(video_id: str, seconds: float) -> str:
    """VOD の指定位置を開く URL (Twitch の ?t=1h2m3s 形式)。"""
    from .download import vod_url

    t = max(0, int(seconds))
    return f"{vod_url(video_id)}?t={t // 3600}h{t % 3600 // 60}m{t % 60}s"


def _make_twitch_clips(cfg: Config, highlights: list[Highlight], clip_vod: tuple[str, float], already: int,
                       owner: str) -> None:
    """Twitch の公式クリップを作る。任意の機能なので、失敗してもショートの結果には影響させない。

    owner: VOD の持ち主 (チャンネル名)。ログイン中のアカウントと一致するときだけ作る。
    """
    from .twitch_auth import TwitchAuthError, UserToken, can_create_clips
    from .twitch_clips import ClipCreator, clips_made_for, create_clips

    vod_id, shift = clip_vod
    registry = Path(cfg.work_dir) / "twitch_clips.json"
    # 同じ VOD を処理し直した分も含めて、1 配信あたりの上限を守る
    limit = max(0, cfg.clips.max_per_stream - max(already, clips_made_for(registry, vod_id)))
    creator = None
    if limit > 0:
        try:
            token = UserToken(cfg.twitch.client_id, cfg.twitch.client_secret, cfg.work_dir)
            # クリップは、クリップ作成の許可がある配信者本人のアカウントでのみ作る
            if cfg.twitch.client_id and owner and can_create_clips(token.token, owner):
                creator = ClipCreator(cfg.twitch.client_id, token)
            else:
                log.warning("Twitch クリップは作りません: %s のクリップを作れるログインがありません "
                            "(配信者本人のアカウントで twitch-shorts login してください)", owner or "この VOD")
        except TwitchAuthError as e:
            log.warning("Twitch クリップは作りません: %s", e)
    try:
        # 新しく作れない場合も、以前作ったクリップは結び付ける
        create_clips(creator, vod_id, highlights, shift, limit, registry=registry)
    except Exception as e:  # 通信エラー等。ショート自体は作れているので続ける
        log.warning("Twitch クリップを作れませんでした: %s", e)


def _hms(t: float, sep: str = "") -> str:
    t = int(t)
    return f"{t // 3600:02d}{sep}{t % 3600 // 60:02d}{sep}{t % 60:02d}"


def _write_report(run_dir: Path, highlights: list[Highlight], channel: str, title: str, duration: float) -> None:
    ordered = sorted(highlights, key=lambda h: h.start)
    data = {
        "channel": channel,
        "stream_title": title,
        "duration": duration,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "highlights": [h.to_dict() for h in ordered],
    }
    (run_dir / "highlights.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
