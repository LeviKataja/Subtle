"""The wire format: `N|text` in, `N|translation` out.

Roughly two tokens of overhead per line. No JSON, no timestamps, no echoed source,
and numbering restarts in every request so the numbers stay single tokens.
"""
from __future__ import annotations

import re
from typing import Callable

_LINE = re.compile(r"^\s*(\d{1,4})\s*[|｜]\s?(.*?)\s*$")


def encode(texts: list[str]) -> str:
    return "\n".join(f"{i}|{t}" for i, t in enumerate(texts, 1))


def _take(line: str, n: int, out: dict[int, str]) -> int | None:
    m = _LINE.match(line)
    if not m:
        return None  # chatter, fences, blank lines: ignored
    i, text = int(m.group(1)) - 1, m.group(2).strip()
    if 0 <= i < n and text and i not in out:
        out[i] = text
        return i
    return None


def parse(text: str, n: int, cut: bool = False) -> dict[int, str]:
    """Reply text -> {zero-based line: translation}. Anything missing is simply absent.

    `cut` says the reply ran into the output limit. Its last line may then be half a
    sentence, so it is left out and gets asked for again like any other missing line.
    """
    out: dict[int, str] = {}
    lines = text.split("\n")
    for line in lines[:-1] if cut else lines:
        _take(line, n, out)
    return out


class StreamParser:
    """Same parser, fed incrementally so lines can be shown the moment they complete."""

    def __init__(self, n: int, on_line: Callable[[int, str], None]):
        self.n, self.on_line = n, on_line
        self.out: dict[int, str] = {}
        self._buf = ""

    def feed(self, chunk: str) -> None:
        self._buf += chunk
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            i = _take(line, self.n, self.out)
            if i is not None:
                self.on_line(i, self.out[i])
