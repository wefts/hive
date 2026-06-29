"""Durable per-viewer conversation log (SQLite, in a private hive volume).

Records what was asked, when, by whom (viewer + scopes), and the answer + its trace
(tier / status / confidence / citations). The content is PRIVATE (a group-scoped
answer may quote private sources), so this store lives only in `hive/` on a mounted
volume — never committed — and is the channel's own, not the kernel's. No new dep
(stdlib sqlite3); WAL mode for the low-concurrency operator console.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from pathlib import Path

_initialized = False


def _db_path() -> str:
    return os.environ.get("WEB_CHANNEL_DB", "/app/data/web_channel.db")


def _conn() -> sqlite3.Connection:
    path = _db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    """Create the table if absent. Safe to call repeatedly."""
    global _initialized
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                viewer TEXT NOT NULL,
                scopes TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                tier TEXT,
                status TEXT,
                confidence REAL,
                citations TEXT,
                asked_at REAL,
                duration_ms INTEGER
            )"""
        )
        # Idempotent migration for stores created before the timing columns existed.
        for col, decl in (("asked_at", "REAL"), ("duration_ms", "INTEGER")):
            with contextlib.suppress(sqlite3.OperationalError):  # column already present
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_conv_viewer ON conversations (viewer, id DESC)")
    _initialized = True


def log_turn(
    viewer: str,
    scopes: list[str],
    question: str,
    answer: str,
    tier: str,
    status: str,
    confidence: float,
    citations: list[dict],
    asked_at: float | None = None,
    duration_ms: int | None = None,
) -> None:
    """Persist one Q&A turn. Best-effort: a logging failure must never break /ask.
    `asked_at` is when the question was submitted; `duration_ms` how long the swarm took."""
    if not _initialized:
        init()
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO conversations "
            "(ts, viewer, scopes, question, answer, tier, status, confidence, citations, "
            "asked_at, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                now,
                viewer,
                ",".join(scopes),
                question,
                answer,
                tier,
                status,
                confidence,
                json.dumps(citations),
                asked_at if asked_at is not None else now,
                duration_ms,
            ),
        )


def recent(viewer: str, limit: int = 20) -> list[dict]:
    """The viewer's most-recent turns (durable history), newest first."""
    if not _initialized:
        init()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ts, question, answer, tier, status, confidence, citations, "
            "asked_at, duration_ms FROM conversations WHERE viewer = ? ORDER BY id DESC LIMIT ?",
            (viewer, limit),
        ).fetchall()
    return [_row_to_turn(r) for r in rows]


def get(viewer: str, conv_id: int) -> dict | None:
    """One past conversation by id, scoped to the viewer (None if not theirs/absent)."""
    if not _initialized:
        init()
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, ts, question, answer, tier, status, confidence, citations, "
            "asked_at, duration_ms FROM conversations WHERE viewer = ? AND id = ?",
            (viewer, conv_id),
        ).fetchone()
    return _row_to_turn(row) if row else None


def _row_to_turn(r) -> dict:
    return {
        "id": r[0],
        "ts": r[1],
        "question": r[2],
        "answer": r[3],
        "tier": r[4],
        "status": r[5],
        "confidence": r[6],
        "citations": json.loads(r[7] or "[]"),
        "asked_at": r[8] if r[8] is not None else r[1],
        "duration_ms": r[9],
    }
