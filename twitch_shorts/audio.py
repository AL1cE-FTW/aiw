"""ffmpeg で音声を取り出し、1 秒ごとの音量(dB)系列を作る。"""

from __future__ import annotations

import json
import subprocess

import numpy as np

SAMPLE_RATE = 8000  # 音量を見るだけなので低サンプルレートで十分


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path],
        check=True, capture_output=True, text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def probe_video_size(path: str) -> tuple[int, int] | None:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "json", path],
        check=True, capture_output=True, text=True,
    ).stdout
    streams = json.loads(out).get("streams") or []
    if not streams:
        return None
    return int(streams[0]["width"]), int(streams[0]["height"])


def loudness_per_second(path: str, start: float = 0.0, duration: float | None = None) -> np.ndarray:
    """ファイルの音声を 1 秒単位の RMS 音量 (dBFS) にする。音声が無ければ空配列。"""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    values: list[float] = []
    block = SAMPLE_RATE * 2  # 1 秒分 (16bit mono)
    leftover = b""
    assert proc.stdout is not None
    # 長時間配信でもメモリを食わないように 1 分ずつ読む
    while True:
        chunk = proc.stdout.read(block * 60)
        if not chunk:
            break
        data = leftover + chunk
        n = len(data) // block
        if n:
            arr = np.frombuffer(data[: n * block], dtype="<i2").astype(np.float32).reshape(n, SAMPLE_RATE)
            rms = np.sqrt(np.mean((arr / 32768.0) ** 2, axis=1))
            values.extend(20 * np.log10(np.maximum(rms, 1e-5)))
        leftover = data[n * block :]
    proc.wait()
    if leftover and len(leftover) >= 2:
        arr = np.frombuffer(leftover[: len(leftover) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
        values.append(float(20 * np.log10(max(np.sqrt(np.mean(arr**2)), 1e-5))))
    if proc.returncode not in (0, None) and not values:
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        if "does not contain any stream" in stderr or "matches no streams" in stderr:
            return np.array([], dtype=np.float32)
        raise RuntimeError(f"音声の解析に失敗しました: {stderr[-500:]}")
    return np.asarray(values, dtype=np.float32)
