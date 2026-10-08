"""パイプライン全体で共有するデータ構造。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


def normalize_hashtag(tag: str) -> str:
    """"#タグ" の形にそろえる (全角の ＃ や、# が無いものも)。空なら空文字。"""
    t = str(tag).strip().lstrip("#＃").strip()
    return f"#{t}" if t else ""


@dataclass
class ChatMessage:
    """配信開始(またはVOD先頭)からの秒数 ``offset`` を持つチャット1件。"""

    offset: float
    user: str
    text: str
    emotes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChatMessage":
        return cls(
            offset=float(d["offset"]),
            user=str(d.get("user", "")),
            text=str(d.get("text", "")),
            emotes=list(d.get("emotes") or []),
        )


@dataclass
class ViewerSample:
    """ある時点 (offset 秒) の同時視聴者数。"""

    offset: float
    viewers: int


@dataclass
class ClipRef:
    """視聴者が作成した既存のTwitchクリップ(VOD上の位置つき)。"""

    offset: float
    duration: float
    views: int
    title: str = ""
    url: str = ""


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class Highlight:
    """検出されたハイライト区間。start/end はソース動画上の秒数。"""

    start: float
    end: float
    peak: float
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    title: str = ""
    hook: str = ""  # 冒頭に出す「引きの言葉」
    category: str = ""  # 面白い / スーパープレイ / ほっこり / ネタ・名場面 / その他
    description: str = ""  # 投稿用の説明文
    hashtags: list[str] = field(default_factory=list)
    reason: str = ""
    chat_sample: list[str] = field(default_factory=list)
    transcript: str = ""
    output_path: str = ""
    video_duration: float = 0.0  # 書き出した動画の実際の長さ (先見せを含む)
    twitch_clip: str = ""  # 作成した Twitch 公式クリップの公開 URL (視聴者に共有できる)
    twitch_clip_edit: str = ""  # そのクリップの編集ページ (配信者本人のみ)
    vod_url: str = ""  # VOD のこの場面を開く URL (手動でクリップするとき用)

    @property
    def duration(self) -> float:
        return self.end - self.start

    def overlaps(self, other: "Highlight", margin: float = 0.0) -> bool:
        return self.start < other.end + margin and other.start < self.end + margin

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 2)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Highlight":
        keys = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in keys})
