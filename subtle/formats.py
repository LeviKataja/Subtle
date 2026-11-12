"""Read and write SRT, WebVTT, ASS/SSA and plain text.

Timing and layout never leave this module: the model only ever sees the words.
"""
from __future__ import annotations

import re

from .model import Cue, Document

EXTENSIONS = (".srt", ".vtt", ".ass", ".ssa", ".txt")

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_TIMING = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}(.*)$")
_SETTING = re.compile(r"^[A-Za-z][\w-]*:\S+$")  # X1:117, line:0, position:20%


def _tail(rest: str) -> tuple[str, str]:
    """What follows the timing on its line -> (position settings, subtitle text).

    Normally that is nothing or a few settings. Some files have the text itself there, and it
    must not be mistaken for settings: the line would vanish from the translation.
    """
    rest = rest.strip()
    if not rest or all(_SETTING.match(word) for word in rest.split()):
        return rest, ""
    return "", rest


def decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")


def _ms(h, m, s, f) -> int:
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(f.ljust(3, "0")[:3])


def _stamp(ms: int, sep: str) -> str:
    h, r = divmod(max(int(ms), 0), 3_600_000)
    m, r = divmod(r, 60_000)
    s, f = divmod(r, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{f:03d}"


def sniff(name: str, text: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in ("srt", "vtt"):
        return ext
    if ext in ("ass", "ssa"):
        return "ass"
    head = text[:4000]
    if head.lstrip().startswith("WEBVTT"):
        return "vtt"
    if "[Script Info]" in head or "[Events]" in text:
        return "ass"
    if any(_TIMING.match(line) for line in head.split("\n")):
        return "srt"
    return "txt"


# --- SRT ------------------------------------------------------------------

def parse_srt(text: str) -> Document:
    cues: list[Cue] = []
    cur: Cue | None = None
    buf: list[str] = []

    def close(another_follows: bool) -> None:
        nonlocal cur, buf
        if cur is not None:
            lines = buf
            while lines and not lines[-1].strip():
                lines.pop()
            # The counter of the *next* cue sits at the end of this one's text.
            if another_follows and lines and lines[-1].strip().isdigit():
                if len(lines) == 1 or not lines[-2].strip() or int(lines[-1]) == len(cues) + 2:
                    lines.pop()
            cur.text = "\n".join(l.strip() for l in lines if l.strip())
            cues.append(cur)
        cur, buf = None, []

    for line in _norm(text).split("\n"):
        m = _TIMING.match(line)
        if m:
            close(True)
            g = m.groups()
            tail, text_here = _tail(g[8])
            cur = Cue(_ms(*g[0:4]), _ms(*g[4:8]), "", {"tail": tail})
            if text_here:
                buf.append(text_here)
        else:
            buf.append(line)
    close(False)
    return Document("srt", cues)


def write_srt(doc: Document, texts: list[str]) -> str:
    out, n = [], 0
    for cue, text in zip(doc.cues, texts):
        if not text.strip():
            continue
        n += 1
        tail = f" {cue.meta['tail']}" if cue.meta.get("tail") else ""
        out.append(f"{n}\n{_stamp(cue.start, ',')} --> {_stamp(cue.end, ',')}{tail}\n{text}\n")
    return "\n".join(out)


# --- WebVTT ---------------------------------------------------------------

def parse_vtt(text: str) -> Document:
    blocks = [b for b in re.split(r"\n[ \t]*\n+", _norm(text).strip("\n")) if b.strip()]
    header = "WEBVTT"
    if blocks and blocks[0].startswith("WEBVTT"):
        header = blocks.pop(0)
    cues: list[Cue] = []
    items: list[list] = []
    for block in blocks:
        lines = block.split("\n")
        ti = next((i for i, l in enumerate(lines[:2]) if _TIMING.match(l)), None)
        if ti is None:
            items.append(["raw", block])
            continue
        g = _TIMING.match(lines[ti]).groups()
        tail, text_here = _tail(g[8])
        body = "\n".join(l.strip() for l in [text_here] + lines[ti + 1:] if l.strip())
        cues.append(Cue(_ms(*g[0:4]), _ms(*g[4:8]), body,
                        {"id": lines[0] if ti == 1 else "", "tail": tail}))
        items.append(["cue", len(cues) - 1])
    return Document("vtt", cues, header, {"items": items})


def write_vtt(doc: Document, texts: list[str]) -> str:
    out = [doc.header or "WEBVTT"]
    for kind, val in doc.extra.get("items", []):
        if kind == "raw":
            out.append(val)
            continue
        cue, text = doc.cues[val], texts[val]
        if not text.strip():
            continue
        tail = f" {cue.meta['tail']}" if cue.meta.get("tail") else ""
        ident = f"{cue.meta['id']}\n" if cue.meta.get("id") else ""
        out.append(f"{ident}{_stamp(cue.start, '.')} --> {_stamp(cue.end, '.')}{tail}\n{text}")
    return "\n\n".join(out) + "\n"


# --- ASS / SSA ------------------------------------------------------------

_ASS_TS = re.compile(r"(\d+):(\d{1,2}):(\d{2})[.:](\d{1,3})")


def _ass_ms(value: str) -> int:
    m = _ASS_TS.search(value or "")
    return _ms(*m.groups()) if m else 0


def parse_ass(text: str) -> Document:
    lines = _norm(text).split("\n")
    fields: list[str] | None = None
    cues: list[Cue] = []
    in_events = False
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith("["):
            in_events = s.lower() == "[events]"
            continue
        if not in_events:
            continue
        low = s.lower()
        if low.startswith("format:"):
            fields = [f.strip().lower() for f in s.split(":", 1)[1].split(",")]
        elif low.startswith("dialogue:") and fields:
            parts = line.split(":", 1)[1].split(",", len(fields) - 1)
            if len(parts) < len(fields):
                continue
            rec = dict(zip(fields, parts))
            body = rec.get("text", "").replace("\\N", "\n").replace("\\n", " ").replace("\\h", " ")
            cues.append(Cue(_ass_ms(rec.get("start", "")), _ass_ms(rec.get("end", "")), body,
                            {"line": i, "style": rec.get("style", "").strip()}))
    return Document("ass", cues, "", {"lines": lines, "nfields": len(fields or [])})


def write_ass(doc: Document, texts: list[str]) -> str:
    lines = list(doc.extra.get("lines", []))
    nfields = doc.extra.get("nfields", 10)
    for cue, text in zip(doc.cues, texts):
        i = cue.meta.get("line")
        if i is None or i >= len(lines):
            continue
        head, _, rest = lines[i].partition(":")
        parts = rest.split(",", nfields - 1)
        parts[-1] = text.replace("\n", "\\N")
        lines[i] = head + ":" + ",".join(parts)
    return "\n".join(lines)


# --- plain text -----------------------------------------------------------

def parse_txt(text: str) -> Document:
    return Document("txt", [Cue(None, None, line) for line in _norm(text).split("\n")])


def write_txt(doc: Document, texts: list[str]) -> str:
    return "\n".join(texts)


# --- dispatch -------------------------------------------------------------

_PARSERS = {"srt": parse_srt, "vtt": parse_vtt, "ass": parse_ass, "txt": parse_txt}
_WRITERS = {"srt": write_srt, "vtt": write_vtt, "ass": write_ass, "txt": write_txt}


def parse(name: str, data: bytes | str) -> Document:
    text = decode(data) if isinstance(data, bytes) else data
    return _PARSERS[sniff(name, text)](text)


def render(doc: Document, bilingual: bool = False) -> str:
    """Serialise the document with translations in place (source text where none exists)."""
    texts = []
    for cue in doc.cues:
        text = cue.translation if cue.translation is not None else cue.text
        if bilingual and doc.fmt != "txt" and cue.translation and cue.translation.strip() and cue.text.strip():
            text = f"{text}\n{' '.join(cue.text.split())}"
        texts.append(text)
    return _WRITERS[doc.fmt](doc, texts)


_LANG_TAGS = {"en", "eng", "english", "en-us", "en-gb", "sdh", "hi", "cc", "forced"}


def output_name(name: str, code: str) -> str:
    """Show.S01E01.en.sdh.srt + fi -> Show.S01E01.fi.srt (the layout media servers expect)."""
    parts = name.split(".")
    ext = parts.pop() if len(parts) > 1 else "srt"
    while len(parts) > 1 and parts[-1].lower() in _LANG_TAGS:
        parts.pop()
    return ".".join(parts + [code or "xx", ext])
