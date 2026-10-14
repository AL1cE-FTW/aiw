"""faster-whisper による文字起こし (任意機能: pip install 'twitch-shorts[transcribe]')。

配信全体ではなくハイライト候補の区間だけを文字起こしするので、長時間配信でも軽い。
"""

from __future__ import annotations

import subprocess

import numpy as np

from .config import TranscribeConfig
from .models import TranscriptSegment


class Transcriber:
    def __init__(self, cfg: TranscribeConfig):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # pragma: no cover - 任意依存
            raise RuntimeError(
                "文字起こしには faster-whisper が必要です: pip install 'twitch-shorts[transcribe]'"
            ) from e
        self.cfg = cfg
        self.model = WhisperModel(cfg.model, device=cfg.device, compute_type=cfg.compute_type)

    def transcribe_range(self, media: str, start: float, end: float) -> list[TranscriptSegment]:
        """``media`` の [start, end) を文字起こしし、ソース上の秒数でセグメントを返す。"""
        # 音声は ffmpeg で 16kHz モノラルの配列にして渡す (faster-whisper 内部の PyAV は版によって動かないため)
        pcm = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{start:.3f}", "-i", media,
             "-t", f"{end - start:.3f}", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
            check=True, capture_output=True,
        ).stdout
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
        segments, _ = self.model.transcribe(
            audio, language=self.cfg.language or None, vad_filter=True, beam_size=5
        )
        return [
            TranscriptSegment(start + s.start, start + s.end, s.text.strip())
            for s in segments
            if s.text.strip()
        ]
