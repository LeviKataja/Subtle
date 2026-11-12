"""Turn cues into the smallest thing worth sending to a model, and back.

Everything here is free: tags, positions, line breaks, sound descriptions, music
symbols and number-only lines are handled locally and never cost a token.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import Cue, Document
from .wrap import wrap

MUSIC = "♪♫♬♩"
_OVR_LEAD = re.compile(r"^(?:\{\\[^}]*\})+")
_OVR_ANY = re.compile(r"\{\\[^}]*\}")
_VTT_JUNK = re.compile(r"</?c(?:\.[^>]*)?>|<\d\d:\d\d[^>]*>|</?v(?:\s[^>]*)?>|</?lang[^>]*>|</?ruby>|</?rt>")
_DASH = re.compile(r"^([-–]\s*)(?=[^\d\s\-–])")
_BRACKET = re.compile(r"\[[^\]\n]*\]")
_PAREN = re.compile(r"\(([^)\n]*)\)")
_SPEAKER = re.compile(r"^[A-Z][A-Z0-9 .'&#-]{1,24}:\s+")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Unit:
    """One line as the model sees it: a speaker turn, or a whole cue."""

    cue: int
    src: str
    kind: str = "text"  # text = translate, keep = pass through untouched
    dash: str = ""  # leading dialogue dash, restored in the original style
    tgt: str | None = None
    state: str = ""  # ok | cached | failed | refused


@dataclass
class Plan:
    """How to put a cue back together once its units are translated."""

    prefix: str = ""
    wraps: list = field(default_factory=list)  # (open, close) tag pairs around the whole cue
    units: list[int] = field(default_factory=list)
    raw: bool = False  # emit the source untouched
    drop: bool = False  # nothing left after stripping: leave it out of the output


def has_letters(text: str) -> bool:
    return any(ch.isalpha() for ch in _TAG.sub("", text))


def has_words(text: str) -> bool:
    """Letters or digits: "3." counts, "." and "..." do not."""
    return any(ch.isalnum() for ch in _TAG.sub("", text))


def wire_text(u: Unit) -> str:
    return ("- " if u.dash else "") + u.src


def wire_tgt(u: Unit) -> str:
    return ("- " if u.dash else "") + (u.tgt or "")


def strip_dash(text: str) -> str:
    m = _DASH.match(text)
    return text[m.end():] if m else text


def _is_lyric(text: str) -> bool:
    t = _TAG.sub("", text).strip()
    return bool(t) and (t[0] in MUSIC or t[-1] in MUSIC or (len(t) > 2 and t[0] == "#" and t[-1] == "#"))


def _strip_wrap(lines: list[str]) -> tuple[list[str], list]:
    """Peel tags that enclose the whole cue (<i>…</i>), so the model never sees them."""
    wraps = []
    while lines:
        joined = "\n".join(lines)
        low = joined.lower()
        for tag in ("i", "b", "u"):
            o, c = f"<{tag}>", f"</{tag}>"
            if all(l.lower().startswith(o) and l.lower().endswith(c) and l.lower().count(o) == 1
                   and len(l) > len(o) + len(c) for l in lines):
                lines = [l[len(o):-len(c)].strip() for l in lines]
            elif low.startswith(o) and low.endswith(c) and low.count(o) == 1:
                lines = [l.strip() for l in joined[len(o):-len(c)].split("\n")]
            else:
                continue
            wraps.append((o, c))
            break
        else:
            m = re.fullmatch(r"(<font\b[^>]*>)(.*)</font>", joined, re.S | re.I)
            if not m or "<font" in m.group(2).lower():
                break
            wraps.append((m.group(1), "</font>"))
            lines = [l.strip() for l in m.group(2).split("\n")]
        lines = [l for l in lines if l]
    return lines, wraps


def _strip_sdh(lines: list[str]) -> list[str]:
    out = []
    for line in lines:
        m = _DASH.match(line)
        dash = m.group(1) if m else ""
        body = before = line[len(dash):]
        body = _BRACKET.sub("", body)
        whole = body.strip()
        body = _PAREN.sub(lambda p: "" if p.group(1).isupper() or p.group(0) == whole else p.group(0), body)
        body = _SPEAKER.sub("", body.strip())
        body = re.sub(r"\s{2,}", " ", body).strip()
        bare = _TAG.sub("", body).strip()
        if not bare or all(ch in MUSIC + " " for ch in bare):
            continue
        if body != before and not any(ch.isalnum() for ch in bare):
            continue  # only punctuation survived the stripping
        out.append(dash + body)
    return out


def prepare(doc: Document, sdh: str = "keep", lyrics: str = "keep") -> tuple[list[Unit], list[Plan]]:
    units: list[Unit] = []
    plans: list[Plan] = []

    def add(plan: Plan, cue_i: int, src: str, kind: str, dash: str = "") -> None:
        units.append(Unit(cue_i, src, kind, dash))
        plan.units.append(len(units) - 1)

    for ci, cue in enumerate(doc.cues):
        plan = Plan()
        plans.append(plan)

        if doc.fmt == "txt":
            body = cue.text.strip()
            if not body:
                plan.raw = True
                continue
            plan.prefix = cue.text[:len(cue.text) - len(cue.text.lstrip())]
            add(plan, ci, body, "text" if has_letters(body) else "keep")
            continue

        text = cue.text.strip()
        m = _OVR_LEAD.match(text)
        if m:
            plan.prefix, text = m.group(0), text[m.end():]
        if re.search(r"\\p[1-9]", plan.prefix):  # ASS vector drawing, not words
            plan.raw = True
            continue
        text = _OVR_ANY.sub("", text)
        if doc.fmt == "vtt":
            text = _VTT_JUNK.sub("", text)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        lines, plan.wraps = _strip_wrap(lines)
        had_text = bool(lines)
        if sdh == "strip":
            lines = _strip_sdh(lines)
        if not lines:
            plan.drop, plan.raw = had_text, not had_text
            continue

        # A dash on any line after the first means two speakers share the cue.
        turns: list[list[str]] = []
        for line in lines:
            m = _DASH.match(line)
            if m:
                turns.append([m.group(1), line[m.end():]])
            elif turns:
                turns[-1][1] += " " + line
            else:
                turns.append(["", line])
        for dash, body in turns:
            keep = not has_letters(body) or (lyrics == "keep" and _is_lyric(body))
            add(plan, ci, body, "keep" if keep else "text", dash)
    return units, plans


def ready(plan: Plan, units: list[Unit]) -> bool:
    return all(units[i].kind == "keep" or units[i].tgt is not None for i in plan.units)


def assemble(cue: Cue, plan: Plan, units: list[Unit], fmt: str, max_chars: int = 42) -> str:
    """Rebuild a cue's output text from its units (source text stands in for anything untranslated)."""
    if plan.raw:
        return cue.text
    if plan.drop:
        return ""
    parts = []
    for i in plan.units:
        u = units[i]
        parts.append(u.dash + (u.tgt if u.tgt is not None and u.kind == "text" else u.src))
    if fmt == "txt":
        return plan.prefix + parts[0]
    lines = parts if len(parts) > 1 else wrap(parts[0], max_chars)
    for o, c in reversed(plan.wraps):
        lines = [o + l + c for l in lines]
    return plan.prefix + "\n".join(lines)


def disassemble(cue: Cue, plan: Plan, units: list[Unit], fmt: str) -> bool:
    """The reverse of assemble: put a cue's current translation back on its units.

    False when there is nothing to work with, or when the cue no longer has the shape its
    plan expects (a hand edit that added or removed a speaker's line).
    """
    mine = [units[i] for i in plan.units]
    text = cue.translation
    if plan.raw or plan.drop or not text or not text.strip() or not any(u.kind == "text" for u in mine):
        return False
    if fmt == "txt":
        mine[0].tgt = text.strip()
        return True
    if plan.prefix and text.startswith(plan.prefix):
        text = text[len(plan.prefix):]
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    for o, c in plan.wraps:  # outermost first
        if all(l.startswith(o) and l.endswith(c) and len(l) >= len(o) + len(c) for l in lines):
            lines = [l[len(o):len(l) - len(c)].strip() for l in lines]
    if len(mine) == 1:
        lines = [" ".join(lines)]
    if len(lines) != len(mine):
        return False
    for u, line in zip(mine, lines):
        if u.kind == "text":
            u.tgt = strip_dash(line).strip() if u.dash else line
    return all(u.tgt for u in mine if u.kind == "text")


def flat(text: str) -> str:
    """Cue text as a single clean line, for use as context."""
    return " ".join(_TAG.sub("", _OVR_ANY.sub("", text)).split())
