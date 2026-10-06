"""パイプライン全体で共有するデータ構造。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


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
    reason: str = ""
    chat_sample: list[str] = field(default_factory=list)
    transcript: str = ""
    output_path: str = ""

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
