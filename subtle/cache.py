"""Translations already paid for are never paid for twice.

Keyed on the exact request (prompt, model, lines, glossary entries used), so a re-run
after an interruption, a retry or a glossary change only re-sends the chunks it affects.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path


class Cache:
    def __init__(self, path: Path | str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS chunks (key TEXT PRIMARY KEY, value TEXT, at REAL)")
        self._db.commit()

    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def get(self, key: str) -> list[str] | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM chunks WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: list[str]) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO chunks VALUES (?, ?, strftime('%s','now'))",
                             (key, json.dumps(value, ensure_ascii=False)))
            self._db.commit()

    def clear(self) -> int:
        with self._lock:
            n = self._db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            self._db.execute("DELETE FROM chunks")
            self._db.commit()
        return n
