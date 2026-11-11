from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Cue:
    """One subtitle event (or one line of a plain text file)."""

    start: int | None  # milliseconds; None for plain text
    end: int | None
    text: str  # source text, lines separated by \n
    meta: dict = field(default_factory=dict)  # format-specific bits needed to write it back
    translation: str | None = None  # assembled output text, lines separated by \n
    flags: list[str] = field(default_factory=list)
    edited: bool = False

    def to_json(self) -> dict:
        return {"s": self.start, "e": self.end, "t": self.text, "m": self.meta,
                "tr": self.translation, "f": self.flags, "ed": self.edited}

    @classmethod
    def from_json(cls, d: dict) -> "Cue":
        return cls(d["s"], d["e"], d["t"], d.get("m") or {}, d.get("tr"), d.get("f") or [], bool(d.get("ed")))


@dataclass
class Document:
    fmt: str  # srt | vtt | ass | txt
    cues: list[Cue]
    header: str = ""
    extra: dict = field(default_factory=dict)  # must stay JSON-serialisable

    def to_json(self) -> dict:
        return {"fmt": self.fmt, "header": self.header, "extra": self.extra,
                "cues": [c.to_json() for c in self.cues]}

    @classmethod
    def from_json(cls, d: dict) -> "Document":
        return cls(d["fmt"], [Cue.from_json(c) for c in d["cues"]], d.get("header", ""), d.get("extra") or {})
