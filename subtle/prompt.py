"""The whole prompt. It is deliberately short: it is sent with every request."""
from __future__ import annotations

import re

from .wire import encode

# Bump when the wording changes, so cached translations made with the old prompt are not reused.
PROMPT_VERSION = 1

_EXAMPLE = """Format example (English to Spanish):
1|I told you yesterday that
2|we can't stay here.
3|- Why not?
becomes
1|Te dije ayer que
2|no podemos quedarnos aquí.
3|- ¿Por qué no?"""

_REGISTER = {
    "informal": "- Characters address each other informally (familiar \"you\") unless a scene clearly calls for formality.",
    "formal": "- Characters address each other formally (polite \"you\") unless a scene is clearly intimate.",
}

_RELEASE = re.compile(r"\b(480p|576p|720p|1080p|2160p|4k|web|webrip|web-dl|bluray|brrip|bdrip|dvdrip|hdtv|"
                      r"x264|x265|h\.?264|h\.?265|hevc|amzn|nf|dsnp|hmax|proper|repack)\b.*", re.I)


def clean_title(name: str) -> str:
    """Show.Name.S01E02.1080p.WEB.x264-GRP.en.srt -> Show Name S01E02"""
    stem = re.sub(r"\.(srt|vtt|ass|ssa|txt)$", "", name, flags=re.I)
    stem = re.sub(r"[._]+", " ", stem)
    stem = _RELEASE.sub("", stem)
    stem = re.sub(r"\s+(en|eng|english|sdh|hi|cc|forced)\b", "", stem, flags=re.I)
    return stem.strip(" -")[:80]


def system_prompt(target: str, source: str = "English", register: str = "auto",
                  notes: str = "", title: str = "", prose: bool = False) -> str:
    what = "lines of a text" if prose else "subtitle lines"
    parts = [
        f"You are a professional {'translator' if prose else 'subtitle translator'}. "
        f"Translate {source} {what} into {target}.",
        "",
        "Input is numbered lines: N|text. Reply with exactly the same numbers, one per line, "
        "as N|translation and nothing else: no notes, no blank lines, no code fences.",
        "",
        "Rules:",
        "- Every number appears exactly once, in order. Never merge, split, skip or leave a line empty.",
        "- A sentence often continues across consecutive numbers. Translate it as one natural sentence, "
        "then divide it over those same numbers, each carrying roughly the matching part.",
    ]
    if prose:
        parts.append(f"- Write natural, idiomatic {target}. Keep the tone, formatting marks and meaning of the original.")
    else:
        parts.append(f"- Write natural, idiomatic spoken {target}, the way a native subtitler would, not word for word. "
                     "Subtitles are read fast, so prefer the shorter phrasing.")
    parts.append("- Keep names, numbers, a leading \"- \" (new speaker), tags such as <i>…</i> and symbols such as ♪ "
                 "where they are. Translate the words inside [brackets].")
    parts.append("- Do not censor, soften, explain or add anything.")
    if register in _REGISTER:
        parts.append(_REGISTER[register])
    if title:
        parts += ["", f"Work: {title}"]
    if notes.strip():
        parts += ["", f"Notes from the user: {notes.strip()}"]
    parts += ["", _EXAMPLE]
    return "\n".join(parts)


def glossary_hits(glossary: list, lines: list[str]) -> list[tuple[str, str]]:
    """Only the entries that actually occur in this request, so a long glossary costs nothing."""
    text = "\n".join(lines)
    hits = []
    for entry in glossary or []:
        if len(entry) < 2 or not str(entry[0]).strip() or not str(entry[1]).strip():
            continue
        src, tgt = str(entry[0]).strip(), str(entry[1]).strip()
        if re.search(r"(?<!\w)" + re.escape(src) + r"(?!\w)", text, re.I):
            hits.append((src, tgt))
    return hits


def user_message(lines: list[str], context: list[tuple[str, str]] | None = None,
                 glossary: list[tuple[str, str]] | None = None, extra: str = "") -> str:
    parts = []
    if glossary:
        parts.append("Glossary (always use these):\n" + "\n".join(f"{s} = {t}" for s, t in glossary))
    if context:
        parts.append("Preceding lines, already translated (context only, do not repeat them):\n"
                     + "\n".join(f"{s} => {t}" for s, t in context))
    if extra:
        parts.append(extra)
    parts.append("Translate:\n" + encode(lines))
    return "\n\n".join(parts)
