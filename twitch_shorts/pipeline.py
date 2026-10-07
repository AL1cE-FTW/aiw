"""検出 → (文字起こし) → (LLM 評価) → レンダリング の一連の処理。"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

import numpy as np

from . import audio as audio_mod
from .config import Config
from .detector import detect_highlights, snap_to_transcript
from .models import ChatMessage, ClipRef, Highlight, TranscriptSegment, ViewerSample
from .render import render_highlight

log = logging.getLogger(__name__)


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


def default_title(cfg: Config, h: Highlight, index: int, channel: str) -> str:
    top = Counter(s.rsplit(" (x", 1)[0] for s in h.chat_sample).most_common(1)
    return cfg.render.title_template.format(
        channel=channel,
        index=index,
        date=datetime.now().strftime("%Y-%m-%d"),
        top_chat=top[0][0] if top else "",
    ).strip()


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
    dry_run: bool = False,
    llm_client=None,
    viewers: list[ViewerSample] | None = None,
) -> RunResult:
    """ハイライトを検出し、ショート動画を ``run_dir`` に書き出す。

    exclude:          既に書き出したハイライト (ライブの逐次処理で重複させないため)
    available_until:  これより後ろにかかる区間は採用しない (録画中のファイル用)
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
    n_candidates = n_target * (cfg.llm.candidate_factor if cfg.llm.enabled else 1) + len(exclude) * 2
    candidates, score = detect_highlights(
        duration, chat, cfg.detect, loudness, clips, top_n=n_candidates, viewers=viewers
    )
    candidates = [
        h for h in candidates
        if not any(h.overlaps(e, margin=5) for e in exclude)
        and (available_until is None or h.end <= available_until)
    ]
    candidates = candidates[: n_target * (cfg.llm.candidate_factor if cfg.llm.enabled else 1)]
    log.info("候補 %d 件を検出", len(candidates))

    transcripts: dict[int, list[TranscriptSegment]] = {}
    if cfg.transcribe.enabled and candidates and apath:
        from .transcribe import Transcriber

        tr = Transcriber(cfg.transcribe)
        for i, h in enumerate(candidates):
            log.info("文字起こし中 (%d/%d)", i + 1, len(candidates))
            segs = tr.transcribe_range(apath, max(0.0, h.start - 5), h.end + 5)
            transcripts[id(h)] = segs
            snap_to_transcript(h, segs, cfg.detect)
            h.transcript = " ".join(s.text for s in segs if s.end > h.start and s.start < h.end)

    if cfg.llm.enabled and candidates:
        from .llm import rerank_with_claude

        log.info("Claude で候補を評価中 (%s)", cfg.llm.model)
        try:
            candidates = rerank_with_claude(
                candidates, cfg.llm, channel, stream_title,
                cfg.detect.min_duration, cfg.detect.max_duration, client=llm_client,
            )
        except Exception as e:
            log.warning("LLM 評価に失敗したため、シグナルのみの順位を使います: %s", e)

    selected = candidates[:n_target]
    # 出力ファイルは時系列順に番号を振る
    selected.sort(key=lambda h: h.start)
    start_index = len(exclude) + 1
    for i, h in enumerate(selected, start=start_index):
        if not h.title:
            h.title = default_title(cfg, h, i, channel)
        if dry_run:
            continue
        video, offset = source.video_for(h.start, h.end)
        out = run_dir / f"short_{i:02d}_{_hms(h.start)}.mp4"
        log.info("書き出し中: %s (%s〜%s) %s", out.name, _hms(h.start, ":"), _hms(h.end, ":"), h.title)
        render_highlight(video, str(out), h, cfg.render, transcripts.get(id(h)), source_offset=offset,
                         max_total=cfg.detect.max_duration)
        h.output_path = str(out)

    _write_report(run_dir, selected + exclude, channel, stream_title, duration)
    if cfg.publish.enabled and not dry_run:
        from .schedule import add_to_schedule

        try:
            for e in add_to_schedule(cfg, selected, channel):
                log.info("投稿予定: %s  %s", e["publish_at"][:16].replace("T", " "), e["title"])
        except (ValueError, KeyError, OSError) as e:  # 設定ミスで書き出し済みの結果を失わないように
            log.warning("投稿予定表に追加できませんでした ([publish] の設定を確認してください): %s", e)
    return RunResult(selected, run_dir, score)


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
