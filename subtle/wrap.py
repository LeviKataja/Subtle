"""Line breaking, done locally so it costs no tokens.

Source line breaks are an artefact of English; the translation is re-broken to fit.
"""
from __future__ import annotations

import math
import re

_PUNCT = ",.;:!?…"
_CJK_PUNCT = "，。、！？；：…"


def _is_spaceless(text: str) -> bool:
    n = sum(1 for ch in text if "぀" <= ch <= "ヿ" or "一" <= ch <= "鿿"
            or "가" <= ch <= "힯" or "฀" <= ch <= "๿")
    return n > len(text) * 0.4


def _wrap_spaceless(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    n = math.ceil(len(text) / limit)
    size = math.ceil(len(text) / n)
    lines, rest = [], text
    while len(rest) > limit and len(lines) < n - 1:
        cut = size
        # Prefer to break just after punctuation, a few characters either side of the even split.
        nearby = [c for c in range(max(2, size - 4), min(size + 4, limit, len(rest) - 1) + 1)
                  if rest[c - 1] in _CJK_PUNCT]
        if nearby:
            cut = min(nearby, key=lambda c: abs(c - size))
        lines.append(rest[:cut])
        rest = rest[cut:]
    lines.append(rest)
    return lines


def wrap(text: str, max_chars: int = 42) -> list[str]:
    """Break one subtitle into balanced lines of at most max_chars (two lines when it fits)."""
    text = " ".join(text.split())
    if not text:
        return [""]
    if _is_spaceless(text):
        return _wrap_spaceless(text, max(12, max_chars // 2))
    if len(text) <= max_chars:
        return [text]

    best = None
    for m in re.finditer(" ", text):
        a, b = text[:m.start()], text[m.start() + 1:]
        over = max(0, len(a) - max_chars) + max(0, len(b) - max_chars)
        score = abs(len(a) - len(b)) + over * 50
        if a[-1] in _PUNCT:
            score -= 10  # a clause boundary is the best place to break
        elif len(a.rsplit(" ", 1)[-1]) <= 3:
            score += 6  # don't strand a short function word at the end of line one
        if len(a) > len(b):
            score += 2  # bottom-heavy reads better
        if best is None or score < best[0]:
            best = (score, a, b)
    if best and max(len(best[1]), len(best[2])) <= max_chars:
        return [best[1], best[2]]

    # Too long for two lines: fill greedily towards an even split.
    n = math.ceil(len(text) / max_chars)
    target = len(text) / n
    lines, cur = [], ""
    for word in text.split(" "):
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) > max_chars or (len(cur) >= target and len(lines) < n - 1):
            lines.append(cur)
            cur = word
        else:
            cur += " " + word
    lines.append(cur)
    return lines
