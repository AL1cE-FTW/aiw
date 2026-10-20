"""ffmpeg で 9:16 の縦型ショート動画を書き出す。

レイアウト:
  - blur:    元映像を中央に置き、背景に同じ映像を拡大・ぼかしたものを敷く (どんな配信でも破綻しない)
  - crop:    元映像の中央を 9:16 で切り抜く (画面中央に見どころがある配信向け)
  - facecam: 上部に顔カメラ (位置は設定で指定)、下部にゲーム画面の中央を並べる
タイトル (上部) と字幕 (下部) は ASS 字幕として焼き込む。
無音カット (話していない間を詰める) とコメント字幕 (チャットをアイコン付きの吹き出しで出す) も付けられる。
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
from .models import ChatMessage, Highlight, TranscriptSegment


def has_audio(path: str) -> bool:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "json", path],
        capture_output=True, text=True, check=True,
    ).stdout
    return bool(json.loads(out).get("streams"))


def _char_width(ch: str) -> float:
    return 1.0 if unicodedata.east_asian_width(ch) in ("W", "F", "A") else 0.55


# 句読点・感嘆符・空白の直後で区切る
_PHRASE_RE = re.compile(r"[^、。！？!?…\s]*[、。！？!?…\s]*")


def _width(text: str) -> float:
    return sum(_char_width(c) for c in text)


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
    # 1 行に収まらない長い単語は文字単位で分ける
    split_tokens: list[str] = []
    for tok in tokens:
        if _width(tok) <= max_width:
            split_tokens.append(tok)
            continue
        piece = ""
        for ch in tok:
            if piece and _width(piece + ch) > max_width:
                split_tokens.append(piece)
                piece = ""
            piece += ch
        split_tokens.append(piece)
    for tok in split_tokens:
        w = _width(tok)
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
            if phrase[-1:].isspace():
                cur += " "  # 次の句と単語がくっつかないように
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


def ass_color(value: str) -> str:
    """"#RRGGBB" を ASS の色 (&H00BBGGRR) にする。"""
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(value).strip())
    if not m:
        raise ValueError(f"色は #RRGGBB で指定してください: {value}")
    rgb = m.group(1).upper()
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def keep_ranges(silences: list[tuple[float, float]], length: float, pad: float,
                min_keep: float = 0.2) -> list[tuple[float, float]]:
    """無音の区間を詰めたあとに残す区間 (区間の頭からの秒数)。前後に pad 秒ずつ残して言葉が切れないようにする。"""
    keep: list[tuple[float, float]] = []
    t = 0.0
    for a, b in sorted(silences):
        # 区間の頭や終わりまで続く無音は、余白を残さずに全部詰める
        cut_a = 0.0 if a <= 0.01 else a + pad
        cut_b = length if b >= length - 0.01 else min(length, b - pad)
        if cut_b - cut_a < 0.3:  # ごく短く詰めると映像が飛んだように見えるだけなので詰めない
            continue
        if cut_a > t:
            keep.append((t, cut_a))
        t = max(t, cut_b)
    if t < length:
        keep.append((t, length))
    keep = [(a, b) for a, b in keep if b - a >= min_keep]
    return keep or [(0.0, length)]


class TimeMap:
    """無音カット前の時刻 (本編の頭からの秒数) を、カット後の時刻に直す。切った間の時刻は次に残す所の頭に寄せる。"""

    def __init__(self, keep: list[tuple[float, float]] | None = None):
        self.keep = keep or []

    def __call__(self, t: float) -> float:
        if not self.keep:
            return t
        out = 0.0
        for a, b in self.keep:
            if t < a:
                return out
            if t <= b:
                return out + t - a
            out += b - a
        return out

    def removed(self, t: float) -> bool:
        """t が切った間 (無音) に入っているか。"""
        return bool(self.keep) and not any(a <= t <= b for a, b in self.keep)


def caption_events(cfg: RenderConfig, duration: float, segments: list[TranscriptSegment], clip_start: float,
                   floor: float, lead_in: float, tmap: TimeMap | None = None) -> list[tuple[float, float, str]]:
    """字幕を (出力動画上の開始, 終了, 文字) の一覧にする。floor より前 (先見せ・フック文の表示中) は出さない。"""
    tmap = tmap or TimeMap()
    cap_chars = min(float(cfg.caption_max_chars), cfg.width * 0.92 / cfg.subtitle_font_size)
    out = []
    for seg in segments:
        a = tmap(seg.start - clip_start) + lead_in
        b = tmap(seg.end - clip_start) + lead_in
        if b <= floor or a >= duration or b - a <= 0:
            continue
        # 発話本来のタイミングで分けてから、隠れる部分を落とす (音声とずれないように)
        for ca, cb, chunk in split_captions(seg.text, a, b, cap_chars):
            ca, cb = max(ca, floor), min(cb, duration)
            if cb - ca >= 0.3:
                out.append((ca, cb, chunk))
    return out


def _rounded_rect(w: float, h: float, r: float) -> str:
    w, h, r = int(w), int(h), int(min(r, h / 2, w / 2))
    return (f"m {r} 0 l {w - r} 0 b {w} 0 {w} 0 {w} {r} l {w} {h - r} b {w} {h} {w} {h} {w - r} {h} "
            f"l {r} {h} b 0 {h} 0 {h} 0 {h - r} l 0 {r} b 0 0 0 0 {r} 0")


def _circle(d: float) -> str:
    r = d / 2
    k = r * 0.5523
    return (f"m {r:.0f} 0 b {r + k:.0f} 0 {d:.0f} {r - k:.0f} {d:.0f} {r:.0f} "
            f"b {d:.0f} {r + k:.0f} {r + k:.0f} {d:.0f} {r:.0f} {d:.0f} "
            f"b {r - k:.0f} {d:.0f} 0 {r + k:.0f} 0 {r:.0f} b 0 {r - k:.0f} {r - k:.0f} 0 {r:.0f} 0")


# アイコンの色 (視聴者ごとに名前から決める)
_ICON_COLORS = ["#F87171", "#FB923C", "#FACC15", "#4ADE80", "#2DD4BF", "#60A5FA", "#A78BFA", "#F472B6"]


def _icon_color(user: str) -> str:
    """文中の色指定 (&HBBGGRR&) で返す。"""
    return "&H" + ass_color(_ICON_COLORS[sum(map(ord, user or "?")) % len(_ICON_COLORS)])[4:] + "&"


def pick_comments(cfg: RenderConfig, chat: list[ChatMessage], clip_start: float, lead_in: float,
                  duration: float, tmap: TimeMap | None = None) -> list[tuple[float, int, ChatMessage]]:
    """画面に出すコメントを (出力動画上の時刻, 段, コメント) で選ぶ。空いている段があるときだけ出す。"""
    tmap = tmap or TimeMap()
    lines = max(1, cfg.comment_lines)
    free_at = [0.0] * lines
    gap = cfg.comment_seconds / lines  # 一度にまとめて出さず、間隔を空ける
    last = -gap
    out = []
    for m in sorted(chat, key=lambda m: m.offset):
        text = m.text.strip()
        rel = m.offset - clip_start
        if not text or text.startswith("!") or "http" in text.lower() or rel < 0 or tmap.removed(rel):
            continue
        t = tmap(rel) + lead_in
        if t + 0.5 > duration or t - last < gap:
            continue
        slot = next((i for i in range(lines) if free_at[i] <= t), None)
        if slot is None:
            continue
        free_at[slot] = t + cfg.comment_seconds
        last = t
        out.append((t, slot, m))
    return out


def comment_dialogues(cfg: RenderConfig, picked: list[tuple[float, int, ChatMessage]], duration: float) -> list[str]:
    """コメントを、角丸の半透明の箱 + 名前の頭文字のアイコン + 本文 の ASS にする。"""
    W, H = cfg.width, cfg.height
    F = cfg.comment_font_size
    icon = int(F * 1.5)
    box_h = icon + 16
    x0 = int(W * 0.04)
    events = []
    for t, slot, m in picked:
        end = min(duration, t + cfg.comment_seconds)
        text = _ass_escape(wrap_text(m.text, float(cfg.comment_max_chars), max_lines=1)[0])
        y = int(H * cfg.comment_position) + slot * (box_h + 12)
        box_w = min(W * 0.92, 8 + icon + 14 + _width(text) * F + 24)
        a, b = _ass_time(t), _ass_time(end)
        fad = r"\fad(150,150)"
        events.append(f"Dialogue: 3,{a},{b},Comment,,0,0,0,,{{\\an7\\pos({x0},{y}){fad}\\p1\\bord0\\shad0"
                      f"\\1c&H000000&\\1a&H60&}}{_rounded_rect(box_w, box_h, box_h / 2)}{{\\p0}}")
        events.append(f"Dialogue: 4,{a},{b},Comment,,0,0,0,,{{\\an7\\pos({x0 + 8},{y + 8}){fad}\\p1\\bord0\\shad0"
                      f"\\1c{_icon_color(m.user)}}}{_circle(icon)}{{\\p0}}")
        initial = _ass_escape((m.user or "?")[:1].upper())
        events.append(f"Dialogue: 5,{a},{b},Comment,,0,0,0,,{{\\an5\\pos({x0 + 8 + icon // 2},{y + 8 + icon // 2})"
                      f"{fad}\\fs{int(icon * 0.55)}\\bord0\\shad0}}{initial}")
        events.append(f"Dialogue: 5,{a},{b},Comment,,0,0,0,,{{\\an4\\pos({x0 + 8 + icon + 14},{y + box_h // 2})"
                      f"{fad}}}{text}")
    return events


def build_ass(cfg: RenderConfig, duration: float, title: str, segments: list[TranscriptSegment],
              clip_start: float, hook_text: str = "", lead_in: float = 0.0,
              keep: list[tuple[float, float]] | None = None, comments: list[ChatMessage] | None = None) -> str:
    """タイトル (上部固定)・フック文・字幕・コメント字幕の ASS を作る。

    lead_in: 冒頭の先見せ (フック) の長さ。字幕はその分だけ後ろにずらす。
    keep:    無音カットで残す本編の区間 (本編の頭からの秒数)。字幕とコメントの時刻をカット後に合わせる
    """
    W, H = cfg.width, cfg.height
    tmap = TimeMap(keep)
    title_chars = W * 0.88 / cfg.title_font_size
    # テロップは中央やや上の決まった位置に、上端を揃えて固定表示する
    # stack レイアウトではゲーム画面を隠さないよう、下のカメラ枠の上端に出す
    cap_y = 1 - cfg.facecam_height_ratio + 0.02 if cfg.layout == "stack" else cfg.caption_position
    pos = rf"{{\an8\pos({W // 2},{int(H * cap_y)})}}"
    sub_color, sub_outline = ass_color(cfg.subtitle_color), ass_color(cfg.subtitle_outline_color)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{cfg.font},{cfg.title_font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,6,2,8,40,40,{int(H * 0.09)},1
Style: Sub,{cfg.font},{cfg.subtitle_font_size},{sub_color},&H00FFFFFF,{sub_outline},&H80000000,1,0,0,0,100,100,0,0,1,6,1,8,30,30,0,1
Style: Hook,{cfg.font},{cfg.hook_font_size},&H0000F0FF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,8,2,8,30,30,0,1
Style: Comment,{cfg.font},{cfg.comment_font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,2,0,4,0,0,0,1

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
    # 先見せ部分とフック文の表示中は字幕を出さない (同じ位置に重なるため)
    floor = max(lead_in, hook_end)
    for ca, cb, chunk in caption_events(cfg, duration, segments, clip_start, floor, lead_in, tmap):
        events.append(f"Dialogue: 0,{_ass_time(ca)},{_ass_time(cb)},Sub,,0,0,0,,{pos}{_ass_escape(chunk)}")
    if comments:
        events += comment_dialogues(cfg, pick_comments(cfg, comments, clip_start, lead_in, duration, tmap), duration)
    return header + "\n".join(events) + "\n"


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(cfg: RenderConfig, duration: float, segments: list[TranscriptSegment], clip_start: float,
              hook_text: str = "", lead_in: float = 0.0, keep: list[tuple[float, float]] | None = None) -> str:
    """書き出した動画に合わせた字幕 (SRT)。フック文も最初に入れる。"""
    events: list[tuple[float, float, str]] = []
    hook_end = 0.0
    if hook_text:
        hook_end = min(duration, max(cfg.hook_text_seconds, lead_in))
        events.append((0.0, hook_end, hook_text))
    events += caption_events(cfg, duration, segments, clip_start, max(lead_in, hook_end), lead_in, TimeMap(keep))
    return "".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n\n" for i, (a, b, text) in enumerate(events, 1))


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
        cam = (f"[cam_src]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},"
               f"scale={W}:{top_h}:force_original_aspect_ratio=increase,crop={W}:{top_h},setsar=1[cam]")
        if cfg.facecam_game_crop > 0:
            # ゲーム画面は横幅のこの割合だけ切り出して収め (拡大しすぎない)、余白はぼかし背景で埋める
            chain = [
                f"{src}split=3[cam_src][game_src][bg_src]", cam,
                f"[game_src]crop=iw*{cfg.facecam_game_crop}:ih,"
                f"scale={W}:{bot_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1[game]",
                f"[bg_src]scale={W // 4}:{bot_h // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{bot_h // 4},"
                f"boxblur=10:2,eq=brightness=-0.12,scale={W}:{bot_h},setsar=1[bg]",
                "[bg][game]overlay=(W-w)/2:(H-h)/2[bottom]",
                "[cam][bottom]vstack=inputs=2[base]",
            ]
        else:
            chain = [
                f"{src}split=2[cam_src][game_src]", cam,
                f"[game_src]scale={W}:{bot_h}:force_original_aspect_ratio=increase,crop={W}:{bot_h},setsar=1[game]",
                "[cam][game]vstack=inputs=2[base]",
            ]
    elif cfg.layout == "stack":
        # 縦 3 段: 上にタイトル用の黒帯、中央にゲーム画面、下にカメラ
        fx, fy, fw, fh = cfg.facecam
        band_h = int(H * cfg.top_band) // 2 * 2
        cam_h = int(H * cfg.facecam_height_ratio) // 2 * 2
        game_h = H - band_h - cam_h
        chain = [
            f"{src}split=2[cam_src][game_src]",
            f"color=c=black:s={W}x{band_h}:r={cfg.fps}:d={duration:.3f}[band]",
            f"[game_src]crop=iw*{cfg.facecam_game_crop or 1}:ih,"
            f"scale={W}:{game_h}:force_original_aspect_ratio=increase,crop={W}:{game_h},setsar=1[game]",
            f"[cam_src]crop=iw*{fw}:ih*{fh}:iw*{fx}:ih*{fy},"
            f"scale={W}:{cam_h}:force_original_aspect_ratio=increase,crop={W}:{cam_h},setsar=1[cam]",
            "[band][game][cam]vstack=inputs=3[base]",
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
        raise ValueError(f"不明なレイアウトです: {cfg.layout} (blur / crop / facecam / stack)")

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
    keep: list[tuple[float, float]] | None = None,
    comments: list[ChatMessage] | None = None,
) -> str:
    """``source`` の [start, end) を縦型ショート動画として ``out_path`` に書き出す。

    hook:     冒頭に先見せする区間 (ソース上の [開始, 終了))。本編の前に連結される。
    keep:     無音カットで残す本編の区間 (本編の頭からの秒数)。None なら全部使う
    comments: コメント字幕に出すチャット (offset はソース上の秒数)
    """
    main_dur = end - start
    if main_dur <= 0:
        raise ValueError("区間の長さが 0 以下です")
    if keep and len(keep) == 1 and keep[0][0] <= 0.01 and keep[0][1] >= main_dur - 0.01:
        keep = None  # 詰める所が無い
    lead_in = (hook[1] - hook[0]) if hook else 0.0
    main_out = sum(b - a for a, b in keep) if keep else main_dur
    duration = lead_in + main_out
    source = os.path.abspath(source)
    out_path = os.path.abspath(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    segs = segments if cfg.subtitles else []
    with tempfile.TemporaryDirectory() as tmp:
        ass_name = None
        chat = comments if cfg.comments else None
        if (cfg.title and title) or segs or hook_text or chat:
            ass_name = "overlay.ass"
            Path(tmp, ass_name).write_text(
                build_ass(cfg, duration, title if cfg.title else "", segs or [], start, hook_text, lead_in,
                          keep=keep, comments=chat),
                encoding="utf-8",
            )
        if cfg.export_srt and (segs or hook_text):
            Path(out_path).with_suffix(".srt").write_text(
                build_srt(cfg, duration, segs or [], start, hook_text, lead_in, keep=keep), encoding="utf-8")
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y",
               "-ss", f"{start:.3f}", "-t", f"{main_dur:.3f}", "-i", source]
        if hook:
            cmd += ["-ss", f"{hook[0]:.3f}", "-t", f"{lead_in:.3f}", "-i", source]
        audio = has_audio(source)
        if not audio:
            cmd += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        silent_idx = 2 if hook else 1

        parts = []
        # 本編: 無音カットするなら残す区間だけを切り出して並べる
        pieces: list[tuple[str, str]] = []
        if keep:
            for i, (a, b) in enumerate(keep):
                parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[mv{i}]")
                if audio:
                    parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[ma{i}]")
                pieces.append((f"[mv{i}]", f"[ma{i}]"))
        else:
            pieces.append(("[0:v]", "[0:a]"))
        if hook:
            pieces.insert(0, ("[1:v]", "[1:a]"))  # 先見せ → 本編 の順に連結する
        if len(pieces) > 1:
            if audio:
                parts.append("".join(v + a for v, a in pieces) + f"concat=n={len(pieces)}:v=1:a=1[cv][ca]")
                v_in, a_in = "[cv]", "[ca]"
            else:
                parts.append("".join(v for v, _ in pieces) + f"concat=n={len(pieces)}:v=1:a=0[cv]")
                v_in, a_in = "[cv]", f"[{silent_idx}:a]"
        else:
            v_in, a_in = pieces[0][0], (pieces[0][1] if audio else f"[{silent_idx}:a]")
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


# 長さ調整で本編の頭を削るとき、盛り上がりのピークの何秒前までは残すか
PEAK_KEEP_BEFORE = 3.0


def hook_range(h: Highlight, cfg: RenderConfig) -> tuple[float, float] | None:
    """盛り上がりのピーク付近から、冒頭に先見せする区間を選ぶ。"""
    if cfg.hook_seconds <= 0:
        return None
    a = max(h.start, h.peak - 0.5)
    b = min(h.end, a + cfg.hook_seconds)
    return (a, b) if b - a >= 0.5 else None


def render_highlight(source: str, out_path: str, h: Highlight, cfg: RenderConfig,
                     segments: list[TranscriptSegment] | None = None, source_offset: float = 0.0,
                     max_total: float | None = None, chat: list[ChatMessage] | None = None,
                     keep: list[tuple[float, float]] | None = None) -> str:
    """ハイライトを書き出す。``source_offset`` はソースファイル先頭が VOD 上の何秒目か。

    先見せを付けて ``max_total`` 秒を超える場合は本編を短くする。基本は頭 (前フリ) を削るが、
    盛り上がりのピークの少し前までしか削らず、それでも長い分は末尾を削る。
    書き出した動画に合わせて ``h.start`` / ``h.end`` / ``h.video_duration`` / ``h.keep`` を更新する。
    keep: 残す区間 (ソース上の秒数) を直接指定する (編集画面で直したカット)。指定すると無音の検出はしない
    """
    hook = hook_range(h, cfg)
    lead_in = (hook[1] - hook[0]) if hook else 0.0
    start, end = h.start, h.end
    if max_total and lead_in + (end - start) > max_total:
        main_len = max_total - lead_in
        start = max(h.start, min(h.end - main_len, h.peak - PEAK_KEEP_BEFORE))
        end = start + main_len
    seg = [TranscriptSegment(s.start - source_offset, s.end - source_offset, s.text) for s in segments or []]
    shifted_hook = (hook[0] - source_offset, hook[1] - source_offset) if hook else None
    if keep is not None:
        keep = [(max(0.0, a - start), min(end, b) - start) for a, b in keep if min(end, b) - max(start, a) > 0.05]
        keep = keep or None
    elif cfg.silence_cut:
        from .audio import silent_ranges

        silences = silent_ranges(source, start - source_offset, end - start, cfg.silence_db, cfg.silence_min)
        keep = keep_ranges(silences, end - start, cfg.silence_pad)
    comments = None
    if cfg.comments and chat:
        comments = [ChatMessage(m.offset - source_offset, m.user, m.text, m.emotes)
                    for m in chat if start <= m.offset < end]
    out = render_short(source, out_path, start - source_offset, end - source_offset, cfg, h.title, seg,
                       hook=shifted_hook, hook_text=h.hook, keep=keep, comments=comments)
    h.start, h.end = round(start, 2), round(end, 2)
    h.keep = [[round(start + a, 2), round(start + b, 2)] for a, b in keep] if keep else []
    main_out = sum(b - a for a, b in keep) if keep else end - start
    h.video_duration = round(lead_in + main_out, 2)
    return out
