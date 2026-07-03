"""Unit tests for the channel-side HS256 actor-assertion signer (ADR-16 D9,
`Swarm.Actor` wire contract). Real interop with the kernel's `Swarm.Actor.verify/2`
was verified manually (`board/journal.md`, 2026-07-02) — these tests cover the
signer's own contract: fail-closed on missing config, correct wire shape."""

from __future__ import annotations

import base64
import json
import time

from web_channel import actor


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def test_sign_returns_none_without_secret(monkeypatch) -> None:
    monkeypatch.delenv("SWARM_ACTOR_SECRET", raising=False)
    assert actor.secret_configured() is False
    assert actor.sign("alice", "local", "sess-1") is None


def test_sign_returns_none_on_incomplete_identity(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    assert actor.sign("", "local", "sess-1") is None
    assert actor.sign("alice", "", "sess-1") is None


def test_sign_produces_a_valid_wire_shape(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    tok = actor.sign("alice", "local", "sess-1")
    assert tok is not None
    parts = tok.split(".")
    assert len(parts) == 3 and all(parts)  # compact JWT: header.payload.signature

    header = json.loads(_b64url_decode(parts[0]))
    assert header == {"alg": "HS256", "typ": "JWT"}

    payload = json.loads(_b64url_decode(parts[1]))
    assert payload["aud"] == "swarm.actor.v1"
    assert payload["sub"] == "alice"
    assert payload["provider"] == "local"
    assert payload["sid"] == "sess-1"
    now = int(time.time())
    assert payload["iat"] <= now
    # Council-accepted MVP posture: short-lived tokens (<= 5 min), never eternal.
    assert 0 < payload["exp"] - payload["iat"] <= 300


def test_sign_never_carries_scopes_or_roles(monkeypatch) -> None:
    # The payload carries only who + session + expiry (per the wire contract) — a
    # forged/replayed token must not be able to widen access by adding fields the
    # kernel would (incorrectly) trust; this asserts the signer itself never emits any.
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    tok = actor.sign("alice", "local", "sess-1")
    assert tok is not None
    payload = json.loads(_b64url_decode(tok.split(".")[1]))
    assert set(payload.keys()) == {"aud", "sub", "provider", "sid", "iat", "exp"}


def test_sign_exp_is_clamped_to_5_minutes(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setenv("SWARM_ACTOR_EXP_S", "3600")  # an operator misconfiguration
    tok = actor.sign("alice", "local", "sess-1")
    assert tok is not None
    payload = json.loads(_b64url_decode(tok.split(".")[1]))
    assert payload["exp"] - payload["iat"] <= 300
