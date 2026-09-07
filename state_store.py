"""Small local database for bot state; no third-party service is required."""

from __future__ import annotations

import sqlite3
from pathlib import Path


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS sent_reminders (key TEXT PRIMARY KEY)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    def get(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    def remember_reminder(self, key: str) -> bool:
        """Return True only on the first attempt for this reminder."""
        with self._connect() as connection:
            try:
                connection.execute("INSERT INTO sent_reminders (key) VALUES (?)", (key,))
            except sqlite3.IntegrityError:
                return False
        return True
