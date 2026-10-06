"""設定(TOML)の読み込み。未指定の項目はデフォルト値を使う。"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass

# 盛り上がりを示すチャット表現と重み。部分一致(小文字化後)で判定する。
DEFAULT_KEYWORDS: dict[str, float] = {
    # 日本語圏
    "草": 1.0,
    "ｗｗ": 1.0,
    "ww": 1.0,
    "笑": 0.6,
    "うおお": 1.2,
    "おおお": 1.0,
    "すげ": 1.0,
    "すご": 0.8,
    "やば": 1.0,
    "ナイス": 1.0,
    "うまい": 0.8,
    "神": 0.8,
    "888": 0.8,
    "！？": 0.8,
    "!?": 0.8,
    "かわいい": 0.6,
    "きた": 0.6,
    # 英語圏 / Twitchエモート
    "lol": 0.8,
    "lmao": 1.0,
    "omg": 1.0,
    "kekw": 1.2,
    "lul": 1.0,
    "pog": 1.2,
    "pogchamp": 1.2,
    "poggers": 1.2,
    "clip": 1.5,
    "クリップ": 1.5,
    "monkas": 0.8,
    "wtf": 1.0,
    "gg": 0.5,
}


@dataclass
class TwitchConfig:
    client_id: str = ""
    client_secret: str = ""
    # ライブチャット(IRC)を認証付きで読む場合のみ。空なら匿名接続。
    irc_oauth_token: str = ""
    irc_nick: str = ""


@dataclass
class DetectConfig:
    weight_chat: float = 1.0
    weight_keywords: float = 0.8
    weight_audio: float = 0.6
    weight_clips: float = 1.2
    # チャットは出来事から数秒遅れて反応するので、その分だけ前にずらす
    chat_delay: float = 6.0
    smooth_seconds: float = 8.0
    # ピーク前後に付ける余白
    pre_roll: float = 18.0
    post_roll: float = 8.0
    min_duration: float = 15.0
    max_duration: float = 59.0
    top_n: int = 5
    min_gap: float = 30.0
    # 総合スコアがこれ未満のピークは採用しない (ランダムな雑談の揺らぎは概ね 4 以下)
    min_score: float = 4.5
    # 最高スコアに対してこの割合未満のピークは採用しない
    min_relative_score: float = 0.3
    keywords: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_KEYWORDS))


@dataclass
class RenderConfig:
    width: int = 1080
    height: int = 1920
    # blur: ぼかし背景+中央に元映像 / crop: 中央を9:16に切り抜き / facecam: 上に顔カメラ・下にゲーム画面
    layout: str = "blur"
    # facecam レイアウトで使う顔カメラ領域(元映像に対する比率 x, y, w, h)
    facecam: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.25, 0.25])
    facecam_height_ratio: float = 0.35
    title: bool = True
    subtitles: bool = True
    font: str = "Noto Sans CJK JP"
    fonts_dir: str = ""
    title_font_size: int = 72
    subtitle_font_size: int = 60
    title_template: str = "{channel} 切り抜き #{index}"
    crf: int = 20
    preset: str = "veryfast"
    fps: int = 30
    loudnorm: bool = True
    fade: float = 0.3


@dataclass
class TranscribeConfig:
    enabled: bool = False
    model: str = "small"
    language: str = "ja"
    device: str = "auto"
    compute_type: str = "int8"


@dataclass
class LLMConfig:
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    # LLMに渡す候補数 = top_n * candidate_factor
    candidate_factor: int = 3
    language: str = "日本語"


@dataclass
class WatchConfig:
    poll_interval: int = 60
    # 0 なら配信終了後にまとめて処理。>0 なら配信中もN分ごとに新しいハイライトを書き出す
    rolling_minutes: int = 0
    recorder: str = "auto"  # auto / streamlink / yt-dlp
    quality: str = "best"


@dataclass
class Config:
    output_dir: str = "output"
    work_dir: str = "work"
    twitch: TwitchConfig = field(default_factory=TwitchConfig)
    detect: DetectConfig = field(default_factory=DetectConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    transcribe: TranscribeConfig = field(default_factory=TranscribeConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)


def _apply(obj, data: dict, path: str = "") -> None:
    valid = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in valid:
            raise ValueError(f"不明な設定項目です: {path}{key}")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"{path}{key} はテーブルで指定してください")
            _apply(current, value, f"{path}{key}.")
        elif key == "keywords" and isinstance(value, dict):
            # キーワードは既定値に追加・上書きする(0 を指定すると無効化)
            merged = dict(current)
            merged.update({str(k).lower(): float(v) for k, v in value.items()})
            setattr(obj, key, {k: v for k, v in merged.items() if v != 0})
        else:
            setattr(obj, key, value)


def load_config(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    if path:
        with open(path, "rb") as f:
            _apply(cfg, tomllib.load(f))
    # 秘密情報は環境変数を優先できるようにする
    cfg.twitch.client_id = os.environ.get("TWITCH_CLIENT_ID", cfg.twitch.client_id)
    cfg.twitch.client_secret = os.environ.get("TWITCH_CLIENT_SECRET", cfg.twitch.client_secret)
    cfg.twitch.irc_oauth_token = os.environ.get("TWITCH_IRC_TOKEN", cfg.twitch.irc_oauth_token)
    cfg.detect.keywords = {k.lower(): v for k, v in cfg.detect.keywords.items()}
    return cfg
