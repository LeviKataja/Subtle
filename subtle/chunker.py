"""Split the lines into requests at scene changes rather than at a fixed count."""
from __future__ import annotations

from .model import Document
from .prep import Unit

_END = ".?!…\"'”)]"


def make_chunks(ids: list[int], units: list[Unit], doc: Document,
                target: int = 120, max_chars: int = 9000) -> list[list[int]]:
    """Group unit ids into chunks of about `target` lines.

    Each boundary lands on the longest pause near the target (a scene change), preferring
    one that follows a finished sentence, and never inside a cue. A request then rarely
    needs context from its neighbours.
    """
    chunks: list[list[int]] = []
    start, n = 0, len(ids)
    while start < n:
        if n - start <= int(target * 1.3):
            chunks.append(ids[start:])
            break
        lo, hi = start + max(1, int(target * 0.6)), min(start + int(target * 1.25), n - 1)
        chars = 0
        for j in range(start, hi + 1):
            chars += len(units[ids[j]].src) + 4
            if chars > max_chars:
                hi = max(j, start + 8)
                break
        lo = min(lo, hi)
        best, best_score = hi, None
        for j in range(lo, hi + 1):  # the chunk would be ids[start:j]
            prev, nxt = units[ids[j - 1]], units[ids[j]]
            if prev.cue == nxt.cue:
                continue
            a, b = doc.cues[prev.cue], doc.cues[nxt.cue]
            if a.end is not None and b.start is not None:
                gap = min(max(b.start - a.end, 0), 8000)
            else:
                gap = 3000 if nxt.cue - prev.cue > 1 else 0  # blank line in plain text
            score = gap - abs(j - (start + target)) * 10
            if prev.src[-1:] in _END:
                score += 4000
            if best_score is None or score > best_score:
                best, best_score = j, score
        chunks.append(ids[start:best])
        start = best
    return chunks
