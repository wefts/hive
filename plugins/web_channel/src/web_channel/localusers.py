"""Channel-owned LOCAL credential store — separate from Keycloak/SSO.

Local (non-SSO) users authenticate against THIS store: pbkdf2-hashed passwords in
the same private SQLite volume as the conversation log. A verified local user yields
a `Principal` of the SAME shape as an OIDC one, so the rest of the app (ask/search/
scope no-leak) is identical regardless of auth source. The kernel is the sole scope
and capability authority (ADR-16 D9 / ADR-20): the row's `scopes` and `is_groot`
columns are legacy bookkeeping kept for schema compatibility, never read as authority.

pbkdf2_hmac (stdlib) keeps this dependency-free; iteration count is generous for a
local operator console. (A prod-grade deployment would prefer argon2/bcrypt.)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from pathlib import Path

from web_channel.auth import PUBLIC_SCOPE, Principal

logger = logging.getLogger("web_channel")
_ITERATIONS = 200_000
_initialized = False


def _safe_scopes(scopes_json: str) -> list[str]:
    """Parse the stored scopes JSON, defaulting to public-only on a malformed row
    (defensive — never crash auth on bad data; council: gemma)."""
    try:
        return _scopes_with_public(json.loads(scopes_json))
    except (ValueError, TypeError):
        logger.warning("malformed scopes row; defaulting to public-only")
        return [PUBLIC_SCOPE]


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
            """CREATE TABLE IF NOT EXISTS local_users (
                username TEXT PRIMARY KEY,
                scopes TEXT NOT NULL,
                is_groot INTEGER NOT NULL DEFAULT 0,
                salt TEXT NOT NULL,
                pwd_hash TEXT NOT NULL,
                created_by TEXT,
                created_at REAL
            )"""
        )
    _initialized = True


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS).hex()


def _scopes_with_public(scopes: list[str]) -> list[str]:
    """Default-deny: public is always present; granted scopes are added, deduped."""
    out = [PUBLIC_SCOPE]
    for s in scopes or []:
        s = s.strip()
        if s and s not in out:
            out.append(s)
    return out


def exists(username: str) -> bool:
    if not _initialized:
        init()
    with _conn() as conn:
        return (
            conn.execute("SELECT 1 FROM local_users WHERE username = ?", (username,)).fetchone()
            is not None
        )


def has_any() -> bool:
    if not _initialized:
        init()
    with _conn() as conn:
        return conn.execute("SELECT 1 FROM local_users LIMIT 1").fetchone() is not None


def create(
    username: str,
    password: str,
    scopes: list[str],
    is_groot: bool = False,
    created_by: str = "",
) -> None:
    """Provision a local user. Raises ValueError if the username already exists."""
    if not _initialized:
        init()
    if exists(username):
        raise ValueError(f"local user already exists: {username}")
    salt = secrets.token_hex(16)
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO local_users "
                "(username, scopes, is_groot, salt, pwd_hash, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    username,
                    json.dumps(_scopes_with_public(scopes)),
                    1 if is_groot else 0,
                    salt,
                    _hash(password, salt),
                    created_by,
                    time.time(),
                ),
            )
    except sqlite3.IntegrityError as e:
        # Lost the exists()→INSERT race; surface as ValueError like the pre-check.
        raise ValueError(f"local user already exists: {username}") from e


def verify(username: str, password: str) -> Principal | None:
    """Return a Principal iff the password matches; else None (constant-time compare)."""
    if not _initialized:
        init()
    with _conn() as conn:
        row = conn.execute(
            "SELECT scopes, is_groot, salt, pwd_hash FROM local_users WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        # Timing parity: a missing user costs ~the same as a present one (a dummy
        # PBKDF2), so /login/local itself doesn't reveal which locals exist (council: codex).
        _hash(password, secrets.token_hex(16))
        return None
    scopes_json, is_groot, salt, pwd_hash = row
    if not hmac.compare_digest(_hash(password, salt), pwd_hash):
        return None
    # `scopes` / `is_groot` on the row are LEGACY bookkeeping, never authority: the kernel
    # derives scopes (Projects) and capabilities (groups + elevation) at ResolveActor
    # (ADR-19/20). Until resolved the principal is public-only and not an admin.
    _ = (scopes_json, is_groot)
    return Principal(
        viewer=username,
        scopes=[PUBLIC_SCOPE],
        groups=[],
        display=username,
        # A local account resolves kernel-side via the SAME identity_link path as
        # SSO (`Swarm.Identity.seed_wheel`/migration): provider "local",
        # subject = the username (ADR-16 D9).
        sub=username,
        provider="local",
    )


def list_users() -> list[dict]:
    """Local users (no hashes) for the groot admin view."""
    if not _initialized:
        init()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT username, scopes, is_groot FROM local_users ORDER BY username"
        ).fetchall()
    return [{"username": r[0], "scopes": _safe_scopes(r[1]), "is_groot": bool(r[2])} for r in rows]
