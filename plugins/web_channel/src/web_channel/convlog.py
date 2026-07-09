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
import secrets
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
        # Idempotent migration for stores created before these columns existed.
        # `ask_ref` is the opaque handle to a retained deliberation (ADR-15), so a
        # reopened past turn can still offer the "see how it decided" affordance.
        # `thread_id` (post-objects rework): a reply carries its ROOT post's row id;
        # NULL = a root post (all pre-rework rows are therefore roots — correct, they
        # were asked without threads). `kernel_conv_id` is the kernel conversation
        # backing the thread's memory (epic 2), set on the root once the dual-write
        # succeeds so replies can continue it.
        # `slug` is the post's PUBLIC short id (YouTube-style, 11 url-safe chars) —
        # the permalink handle (/p/{slug}); the integer row id stays internal.
        for col, decl in (
            ("asked_at", "REAL"),
            ("duration_ms", "INTEGER"),
            ("ask_ref", "TEXT"),
            ("thread_id", "INTEGER"),
            ("kernel_conv_id", "TEXT"),
            ("slug", "TEXT"),
        ):
            with contextlib.suppress(sqlite3.OperationalError):  # column already present
                conn.execute(f"ALTER TABLE conversations ADD COLUMN {col} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_conv_viewer ON conversations (viewer, id DESC)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_conv_thread ON conversations (viewer, thread_id, id)"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_conv_slug ON conversations (slug)")
        # Backfill: rows from before slugs existed each get one (idempotent — only
        # NULL rows), so every old post is permalinkable too.
        for (row_id,) in conn.execute("SELECT id FROM conversations WHERE slug IS NULL").fetchall():
            conn.execute("UPDATE conversations SET slug = ? WHERE id = ?", (new_slug(), row_id))
    _initialized = True


def new_slug() -> str:
    """A fresh public post id — 11 url-safe chars (YouTube-shaped), collision-safe
    in practice (64 bits) and guarded by the unique index anyway."""
    return secrets.token_urlsafe(8)


_TURN_COLS = (
    "id, ts, question, answer, tier, status, confidence, citations, "
    "asked_at, duration_ms, ask_ref, thread_id, kernel_conv_id, slug"
)


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
    ask_ref: str = "",
    thread_id: int | None = None,
    kernel_conv_id: str = "",
    slug: str = "",
) -> int:
    """Persist one Q&A turn; returns the new row id (a root post's id IS its thread
    handle). Best-effort at the call site: a logging failure must never break /ask.
    `thread_id` is the root post's row id for a reply, None for a root post;
    `kernel_conv_id` is the kernel conversation backing the thread's memory; `slug`
    is the public permalink id (pre-minted by /ask/start so the pushed URL matches —
    minted here when absent: every turn is a first-class, addressable post)."""
    if not _initialized:
        init()
    now = time.time()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO conversations "
            "(ts, viewer, scopes, question, answer, tier, status, confidence, citations, "
            "asked_at, duration_ms, ask_ref, thread_id, kernel_conv_id, slug) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ask_ref,
                thread_id,
                kernel_conv_id,
                slug or new_slug(),
            ),
        )
        return int(cur.lastrowid or 0)


def get_by_slug(viewer: str, slug: str) -> dict | None:
    """One turn by its public slug, scoped to the viewer (None if not theirs/absent)."""
    if not _initialized:
        init()
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_TURN_COLS} FROM conversations WHERE viewer = ? AND slug = ?",
            (viewer, slug),
        ).fetchone()
    return _row_to_turn(row) if row else None


def set_kernel_conv(viewer: str, root_id: int, kernel_conv_id: str) -> None:
    """Record the kernel conversation backing a root post's thread (viewer-scoped) —
    set once the dual-write succeeds, so later replies continue the same memory."""
    if not _initialized:
        init()
    with _conn() as conn:
        conn.execute(
            "UPDATE conversations SET kernel_conv_id = ? WHERE viewer = ? AND id = ?",
            (kernel_conv_id, viewer, root_id),
        )


def recent(viewer: str, limit: int = 20) -> list[dict]:
    """The viewer's most-recent ROOT posts (durable history), newest first. Replies
    live under their root (`thread`), never as standalone history entries."""
    if not _initialized:
        init()
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_TURN_COLS} FROM conversations WHERE viewer = ? AND thread_id IS NULL "
            "ORDER BY id DESC LIMIT ?",
            (viewer, limit),
        ).fetchall()
    return [_row_to_turn(r) for r in rows]


def get(viewer: str, conv_id: int) -> dict | None:
    """One past turn by id, scoped to the viewer (None if not theirs/absent)."""
    if not _initialized:
        init()
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_TURN_COLS} FROM conversations WHERE viewer = ? AND id = ?",
            (viewer, conv_id),
        ).fetchone()
    return _row_to_turn(row) if row else None


def replies(viewer: str, root_id: int) -> list[dict]:
    """A root post's replies, oldest first (forum order), scoped to the viewer."""
    if not _initialized:
        init()
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_TURN_COLS} FROM conversations WHERE viewer = ? AND thread_id = ? "
            "ORDER BY id ASC",
            (viewer, root_id),
        ).fetchall()
    return [_row_to_turn(r) for r in rows]


def last_turn(viewer: str, root_id: int) -> dict | None:
    """The thread's most recent turn (the last reply, or the root itself) — the
    context source for a follow-up's active_keys (epic 2, per-thread)."""
    if not _initialized:
        init()
    with _conn() as conn:
        row = conn.execute(
            f"SELECT {_TURN_COLS} FROM conversations WHERE viewer = ? "
            "AND (id = ? OR thread_id = ?) ORDER BY id DESC LIMIT 1",
            (viewer, root_id, root_id),
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
        "ask_ref": r[10] or "",
        "thread_id": r[11],
        "kernel_conv_id": r[12] or "",
        "slug": r[13] or "",
    }
