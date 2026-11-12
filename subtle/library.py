"""What each video in the library has: real subtitles, a translation of ours, or nothing yet.

The files on disk are the truth, so nothing here remembers "this episode is translated": that
is read off the folder every time. The one thing a file cannot say about itself is who wrote
it, so the subtitle files Subtle writes are recorded, with what they cost.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from . import media
from .languages import resolve, variants

_UNLABELLED = {"und", "unk", "zxx", "mis"}


class Records:
    """The subtitle files Subtle wrote, keyed by path."""

    FIELDS = ("size", "model", "reader", "cost", "lines", "flagged", "changed", "at")

    def __init__(self, path: Path | str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS made (out TEXT PRIMARY KEY, size INTEGER, model TEXT, "
                         "reader TEXT, cost REAL, lines INTEGER, flagged INTEGER, changed INTEGER, at REAL)")
        self._db.commit()

    @staticmethod
    def _key(out: Path | str) -> str:
        return os.path.normcase(os.path.abspath(str(out)))

    def put(self, out: Path | str, model: str = "", reader: str = "", cost: float = 0.0, lines: int = 0,
            flagged: int = 0, changed: int = 0) -> None:
        try:
            size = Path(out).stat().st_size
        except OSError:
            return
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO made VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (self._key(out), size, model, reader, cost, lines, flagged, changed, time.time()))
            self._db.commit()

    def get(self, out: Path | str) -> dict | None:
        """The record for this file, or None if Subtle did not write what is there now.

        A file whose size has changed was replaced by something else (real subtitles from
        Bazarr, say), so it no longer counts as ours.
        """
        with self._lock:
            row = self._db.execute(f"SELECT {', '.join(self.FIELDS)} FROM made WHERE out = ?",
                                   (self._key(out),)).fetchone()
        if not row:
            return None
        try:
            if Path(out).stat().st_size != row[0]:
                return None
        except OSError:
            return None
        return dict(zip(self.FIELDS, row))


def status(video: Path, target: str, inside: list[str] | None = None, records: Records | None = None,
           source: str = "en") -> dict:
    """Where one video stands for the target language.

    state: have (real subtitles) | ours (Subtle wrote them) | missing (can be translated)
           | nosource (nothing to translate from) | gone (the file cannot be opened here)
    `inside` is what Sonarr or Radarr list as subtitle languages inside the file (None if they
    have not scanned it). That list cannot tell text from pictures, so "missing" means worth
    trying, not certain to work.
    """
    unknown = inside is None
    inside = [c.lower() for c in inside or []]
    if not video.is_file():
        return {"state": "gone", "note": f"Cannot open {video}. Check the path mapping."}
    want = variants(target)
    for path, tags in media.sidecars(video):
        if any(t in want for t in tags) and "forced" not in tags:
            made = records.get(path) if records else None
            if made:
                return {"state": "ours", "note": path.name, "made": made}
            return {"state": "have", "note": path.name}
    if any(c in want for c in inside):
        return {"state": "have", "note": "a track inside the file"}
    from_here = variants(source)
    if unknown or media.find_sidecar(video, source) or any(c in from_here or c in _UNLABELLED for c in inside):
        return {"state": "missing", "note": ""}
    if inside:
        name = resolve(source)[1] or source
        return {"state": "nosource", "note": f"No {name} subtitles to translate from (inside: {', '.join(inside)})."}
    return {"state": "nosource", "note": "No subtitles to translate from."}
