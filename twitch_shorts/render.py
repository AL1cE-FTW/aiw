"""ffmpeg で 9:16 の縦型ショート動画を書き出す。

レイアウト:
  - blur:    元映像を中央に置き、背景に同じ映像を拡大・ぼかしたものを敷く (どんな配信でも破綻しない)
  - crop:    元映像の中央を 9:16 で切り抜く (画面中央に見どころがある配信向け)
  - facecam: 上部に顔カメラ (位置は設定で指定)、下部にゲーム画面の中央を並べる
タイトル (上部) と字幕 (下部) は ASS 字幕として焼き込む。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

from .config import RenderConfig
from .models import Highlight, TranscriptSegment


def has_audio(path: str) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return bool(json.loads(out).get("streams"))


def _char_width(ch: str) -> float:
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.55


def wrap_text(text: str, max_width: float, max_lines: int = 3) -> list[str]:
    """全角を 1、半角を約 0.55 として幅を数え、単語をなるべく割らずに折り返す。"""
    text = " ".join(text.split())
    lines: list[str] = []
    cur, cur_w = "", 0.0
    tokens: list[str] = []
    word = ""
    for ch in text:
        if _char_width(ch) < 1.0 and not ch.isspace():
            word += ch  # 半角英数字は単語としてまとめる
            continue
        if word:
            tokens.append(word)
            word = ""
        tokens.append(ch)
    if word:
        tokens.append(word)
    for tok in tokens:
        w = sum(_char_width(c) for c in tok)
        if cur_w + w > max_width and cur.strip():
            lines.append(cur.strip())
            cur, cur_w = "", 0.0
            if tok.isspace():
                continue
        cur += tok
        cur_w += w
    if cur.strip():
        lines.append(cur.strip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def _ass_escape(text: str) -> str:
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝").replace("\n", " ")


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# 句読点・感嘆符・空白の直後で区切る
_PHRASE_RE = re.compile(r"[^、。！？!?…\s]*[、。！？!?…\s]*")


def _width(text: str) -> float:
    return sum(_char_width(c) for c in text)


def split_captions(text: str, start: float, end: float, max_chars: float) -> list[tuple[float, float, str]]:
    """長い発話を max_chars 以内のテロップに分け、文字量に応じて表示時間を割り振る。"""
    chunks: list[str] = []
    cur = ""
    for phrase in _PHRASE_RE.findall(" ".join(text.split())):
        if not phrase.strip():
            continue
        if _width((cur + phrase).strip()) <= max_chars:
            cur += phrase
            continue
        if cur.strip():
            chunks.append(cur.strip())
        if _width(phrase.strip()) <= max_chars:
            cur = phrase
        else:  # 区切りの無い長い句だけは文字数で切る
            *full, cur = wrap_text(phrase, max_chars, max_lines=1000)
            chunks.extend(full)
    if cur.strip():
        chunks.append(cur.strip())
    if not chunks:
        return []
    widths = [max(_width(ch), 1.0) for ch in chunks]
    total = sum(widths)
    out, t = [], start
    for ch, w in zip(chunks, widths):
        d = (end - start) * w / total
        out.append((t, t + d, ch))
        t += d
    return out


def build_ass(cfg: RenderConfig, duration: float, title: str, segments: list[TranscriptSegment],
              clip_start: float, hook_text: str = "", lead_in: float = 0.0) -> str:
    """タイトル (上部固定)・フック文・字幕の ASS を作る。

    lead_in: 冒頭の先見せ (フック) の長さ。字幕はその分だけ後ろにずらす。
    """
    W, H = cfg.width, cfg.height
    title_chars = W * 0.88 / cfg.title_font_size
    cap_chars = min(float(cfg.caption_max_chars), W * 0.92 / cfg.subtitle_font_size)
    # テロップは中央やや上の決まった位置に、上端を揃えて固定表示する
    pos = rf"{{\an8\pos({W // 2},{int(H * cfg.caption_position)})}}"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{cfg.font},{cfg.title_font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,2,8,40,40,{int(H * 0.09)},1
Style: Sub,{cfg.font},{cfg.subtitle_font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,1,8,30,30,0,1
Style: Hook,{cfg.font},{cfg.hook_font_size},&H0000F0FF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,2,8,30,30,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    if title:
        text = r"\N".join(_ass_escape(l) for l in wrap_text(title, title_chars, max_lines=2))
        events.append(f"Dialogue: 1,{_ass_time(0)},{_ass_time(duration)},Title,,0,0,0,,{text}")
    hook_end = 0.0
    if hook_text:
        hook_end = min(duration, max(cfg.hook_text_seconds, lead_in))
        lines = wrap_text(hook_text, min(float(cfg.caption_max_chars), W * 0.92 / cfg.hook_font_size), max_lines=2)
        text = r"\N".join(_ass_escape(l) for l in lines)
        events.append(f"Dialogue: 2,{_ass_time(0)},{_ass_time(hook_end)},Hook,,0,0,0,,{pos}{text}")
    for seg in segments:
        a = seg.start - clip_start + lead_in
        b = seg.end - clip_start + lead_in
        # 先見せ部分とフック文の表示中は字幕を出さない (同じ位置に重なるため)
        a = max(a, lead_in, hook_end)
        b = min(b, duration)
        if b - a < 0.3:
            continue
        for ca, cb, chunk in split_captions(seg.text, a, b, cap_chars):
            events.append(f"Dialogue: 0,{_ass_time(ca)},{_ass_time(cb)},Sub,,0,0,0,,{pos}{_ass_escape(chunk)}")
    return header + "\n".join(events) + "\n"


def _filter_escape(value: str) -> str:
    return "'" + value.replace("\\", "/").replace("'", r"\'").replace(":", r"\:") + "'"


def build_video_filter(cfg: RenderConfig, ass_name: str | None, duration: float, src: str = "[0:v]") -> str:
    W, H = cfg.width, cfg.height
    if cfg.layout == "crop":
        chain = [f"{src}scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[base]"]
    elif cfg.layout == "facecam":
        fx, fy, fw, fh = cfg.facecam
        top_h = int(H * cfg.facecam_height_ratio) // 2 * 2
        bot_h = H - top_h
        chain = [
            f"{src}split=2[cam_src][game_src]",
            f"[cam_src]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},"
            f"scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h},setsar=1[cam]",
            f"[game_src]scale={W}:{bot_h}:force_original_aspect_ratio=increase,crop={W}:{bot_h},setsar=1[game]",
            "[cam][game]vstack=inputs=2[base]",
        ]
    elif cfg.layout == "blur":
        chain = [
            f"{src}split=2[bg_src][fg_src]",
            f"[bg_src]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
            f"boxblur=10:2,eq=brightness=-0.12,scale={W}:{H},setsar=1[bg]",
            f"[fg_src]scale={W}:{H}:force_original_aspect_ratio=decrease,setsar=1[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[base]",
        ]
    else:
        raise ValueError(f"不明なレイアウトです: {cfg.layout} (blur / crop / facecam)")

    post = [f"fps={cfg.fps}"]
    if ass_name:
        sub = f"subtitles={_filter_escape(ass_name)}"
        if cfg.fonts_dir:
            sub += f":fontsdir={_filter_escape(os.path.abspath(cfg.fonts_dir))}"
        post.append(sub)
    if cfg.fade > 0:
        post.append(f"fade=t=in:st=0:d={cfg.fade}")
        post.append(f"fade=t=out:st={max(0.0, duration - cfg.fade):.3f}:d={cfg.fade}")
    post.append("format=yuv420p")
    chain.append(f"[base]{','.join(post)}[v]")
    return ";".join(chain)


def build_audio_filter(cfg: RenderConfig, duration: float) -> str:
    parts = ["aresample=48000"]
    if cfg.fade > 0:
        parts.append(f"afade=t=in:st=0:d={cfg.fade}")
        parts.append(f"afade=t=out:st={max(0.0, duration - cfg.fade):.3f}:d={cfg.fade}")
    if cfg.loudnorm:
        # ショート動画プラットフォームの基準 (-14 LUFS 前後) に合わせる
        parts.append("loudnorm=I=-14:TP=-1.5:LRA=11")
    return ",".join(parts)


def render_short(
    source: str,
    out_path: str,
    start: float,
    end: float,
    cfg: RenderConfig,
    title: str = "",
    segments: list[TranscriptSegment] | None = None,
    hook: tuple[float, float] | None = None,
    hook_text: str = "",
) -> str:
    """``source`` の [start, end) を縦型ショート動画として ``out_path`` に書き出す。

    hook: 冒頭に先見せする区間 (ソース上の [開始, 終了))。本編の前に連結される。
    """
    main_dur = end - start
    if main_dur <= 0:
        raise ValueError("区間の長さが 0 以下です")
    lead_in = (hook[1] - hook[0]) if hook else 0.0
    duration = lead_in + main_dur
    source = os.path.abspath(source)
    out_path = os.path.abspath(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    segs = segments if cfg.subtitles else []
    with tempfile.TemporaryDirectory() as tmp:
        ass_name = None
        if (cfg.title and title) or segs or hook_text:
            ass_name = "overlay.ass"
            Path(tmp, ass_name).write_text(
                build_ass(cfg, duration, title if cfg.title else "", segs or [], start, hook_text, lead_in),
                encoding="utf-8",
            )
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y",
               "-ss", f"{start:.3f}", "-t", f"{main_dur:.3f}", "-i", source]
        if hook:
            cmd += ["-ss", f"{hook[0]:.3f}", "-t", f"{lead_in:.3f}", "-i", source]
        audio = has_audio(source)
        if not audio:
            cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        silent_idx = 2 if hook else 1

        parts = []
        if hook:
            # 先見せ → 本編 の順に連結する
            if audio:
                parts.append("[1:v][1:a][0:v][0:a]concat=n=2:v=1:a=1[cv][ca]")
                v_in, a_in = "[cv]", "[ca]"
            else:
                parts.append("[1:v][0:v]concat=n=2:v=1:a=0[cv]")
                v_in, a_in = "[cv]", f"[{silent_idx}:a]"
        else:
            v_in, a_in = "[0:v]", ("[0:a]" if audio else f"[{silent_idx}:a]")
        parts.append(build_video_filter(cfg, ass_name, duration, v_in))
        parts.append(f"{a_in}{build_audio_filter(cfg, duration)}[a]")
        cmd += [
            "-filter_complex", ";".join(parts),
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", cfg.preset, "-crf", str(cfg.crf),
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-t", f"{duration:.3f}",
            "-movflags", "+faststart",
            out_path,
        ]
        # 字幕ファイルを相対パスで参照するため、一時ディレクトリをカレントにして実行する
        proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg が失敗しました:\n{proc.stderr[-2000:]}")
    return out_path


def hook_range(h: Highlight, cfg: RenderConfig) -> tuple[float, float] | None:
    """盛り上がりのピーク付近から、冒頭に先見せする区間を選ぶ。"""
    if cfg.hook_seconds <= 0:
        return None
    a = max(h.start, h.peak - 0.5)
    b = min(h.end, a + cfg.hook_seconds)
    return (a, b) if b - a >= 0.5 else None


def render_highlight(source: str, out_path: str, h: Highlight, cfg: RenderConfig,
                     segments: list[TranscriptSegment] | None = None, source_offset: float = 0.0,
                     max_total: float | None = None) -> str:
    """ハイライトを書き出す。``source_offset`` はソースファイル先頭が VOD 上の何秒目か。

    先見せを付けて ``max_total`` 秒を超える場合は、本編の頭 (前フリ) を削って収める。
    """
    hook = hook_range(h, cfg)
    start = h.start
    if hook and max_total and (hook[1] - hook[0]) + (h.end - start) > max_total:
        start = h.end - (max_total - (hook[1] - hook[0]))
    seg = [TranscriptSegment(s.start - source_offset, s.end - source_offset, s.text) for s in segments or []]
    shifted_hook = (hook[0] - source_offset, hook[1] - source_offset) if hook else None
    return render_short(source, out_path, start - source_offset, h.end - source_offset, cfg, h.title, seg,
                        hook=shifted_hook, hook_text=h.hook)
