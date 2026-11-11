"""Quality checks that run locally and cost nothing.

They only look at a cue's source and translation, so they can be re-run after a manual edit.
"""
from __future__ import annotations

import re

from .model import Cue

ERRORS = {"failed", "refused", "untranslated"}
LABELS = {
    "failed": "Not translated: the model kept skipping this line",
    "refused": "The model declined to translate this line",
    "untranslated": "Identical to the source",
    "english": "Still looks like English",
    "fast": "Too much text for the time on screen",
    "long": "Line longer than the limit",
    "ratio": "Length is far off the source",
    "tags": "Formatting tags differ from the source",
}

_TAG = re.compile(r"<[^>]+>|\{\\[^}]*\}")
_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
_ENGLISH = {
    "the", "and", "you", "that", "with", "they", "this", "have", "from", "what", "were", "when", "your",
    "there", "been", "would", "about", "don't", "it's", "i'm", "here", "where", "them", "just", "know",
    "going", "think", "right", "could", "should", "their", "which", "because", "something", "nothing",
    "never", "really", "please", "can't", "didn't", "you're", "that's", "we're", "isn't", "wasn't",
}


def visible(text: str) -> str:
    return _TAG.sub("", text or "")


def check(cue: Cue, target_code: str = "", fmt: str = "srt", max_chars: int = 42, max_cps: int = 21,
          carry: tuple[str, ...] = ()) -> list[str]:
    """Flags for one cue. `carry` holds flags only the engine can know (failed, refused)."""
    flags = list(carry)
    src, tr = visible(cue.text).strip(), visible(cue.translation or "").strip()
    if not src or cue.translation is None or (not tr and "failed" not in flags):
        return flags
    src_flat, tr_flat = " ".join(src.split()), " ".join(tr.split())
    words = _WORD.findall(tr_flat)
    not_english = not target_code.lower().startswith("en")

    if not flags and not_english and src_flat.lower() == tr_flat.lower():
        src_words = _WORD.findall(src_flat)
        if len(src_words) >= 2 and not all(w[:1].isupper() for w in src_words):
            flags.append("untranslated")
    if not flags and not_english and len(words) >= 4:
        if sum(1 for w in words if w.lower() in _ENGLISH) / len(words) >= 0.5:
            flags.append("english")

    if fmt != "txt":
        lines = tr.split("\n")
        if any(len(l) > max_chars for l in lines) or len(lines) > 3:
            flags.append("long")
        if cue.start is not None and cue.end is not None and cue.end - cue.start > 300:
            # Only when the translation made it worse: a source that was already dense is not our doing.
            too_fast = len(tr_flat) / ((cue.end - cue.start) / 1000) > max_cps
            if too_fast and len(tr_flat) > 12 and len(tr_flat) > len(src_flat) * 1.1:
                flags.append("fast")
        tags = lambda t: {x.lower() for x in re.findall(r"</?[a-zA-Z]+", t or "")}
        if tags(cue.text) != tags(cue.translation):
            flags.append("tags")
    if len(src_flat) >= 25 and (len(tr_flat) > len(src_flat) * 2.4 or len(tr_flat) < len(src_flat) * 0.3):
        flags.append("ratio")
    return flags


def cps(cue: Cue) -> float | None:
    if cue.start is None or cue.end is None or cue.end <= cue.start or not cue.translation:
        return None
    return len(" ".join(visible(cue.translation).split())) / ((cue.end - cue.start) / 1000)
