"""Operator-managed runtime settings in the channel's private SQLite store."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

_initialized = False
_callbacks: list[Callable[[], None]] = []


def _db_path() -> str:
    return os.environ.get("WEB_CHANNEL_DB", "/app/data/web_channel.db")


def _conn() -> sqlite3.Connection:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    global _initialized
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
    _initialized = True


def get(key: str) -> str | None:
    if not _initialized:
        init()
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row[0]) if row else None


def get_or_env(key: str, default: str = "") -> str:
    stored = get(key)
    if stored is not None:
        return stored
    return os.environ.get(key, default)


def put(key: str, value: str) -> None:
    if not _initialized:
        init()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    for fn in list(_callbacks):
        fn()


def register_on_change(fn: Callable[[], None]) -> None:
    _callbacks.append(fn)
