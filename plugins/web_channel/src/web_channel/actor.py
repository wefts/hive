"""Sign the actor assertion the kernel verifies (workspace ADR-16 Decision 9 —
the crux). Mirrors `Swarm.Actor`'s wire contract VERBATIM (`swarm/kernel/lib/
swarm/actor.ex` moduledoc): a compact HS256 JWT, `aud` pinned to
"swarm.actor.v1", payload `{sub, provider, sid, iat, exp}` — never scopes/roles/
uuid (those are derived kernel-side from its own records, so a forged or
replayed token cannot widen access).

The channel is the ONLY signer (`Swarm.Actor.sign/2` in the kernel is the
reference implementation used in kernel tests, not a production signing path).
Verification stays kernel-side; this module has no verify function by design.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

AUDIENCE = "swarm.actor.v1"
_HEADER = {"alg": "HS256", "typ": "JWT"}
_MAX_EXP_S = 300  # council (codex+llama3.3:70b) accepted-MVP posture: <= 5 min,
# bounding the replay/logout-lag window (no session-revocation mechanism yet).


def _exp_s() -> int:
    return min(_MAX_EXP_S, int(os.environ.get("SWARM_ACTOR_EXP_S", str(_MAX_EXP_S))))


def secret_configured() -> bool:
    return bool(os.environ.get("SWARM_ACTOR_SECRET", "").strip())


def _secret() -> bytes | None:
    s = os.environ.get("SWARM_ACTOR_SECRET", "").strip()
    return s.encode() if s else None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign(sub: str, provider: str, sid: str) -> str | None:
    """Sign a fresh short-lived actor assertion, or None when signing is not
    possible (no secret configured, or an incomplete identity) — callers fall
    back to the legacy dual-accept plaintext viewer (`Auth.legacy_context/2`
    on the kernel side tolerates this during the migration window)."""
    secret = _secret()
    if not secret or not sub or not provider:
        return None
    now = int(time.time())
    payload = {
        "aud": AUDIENCE,
        "sub": sub,
        "provider": provider,
        "sid": sid,
        "iat": now,
        "exp": now + _exp_s(),
    }
    signing_input = (
        f"{_b64(json.dumps(_HEADER, separators=(',', ':')).encode())}."
        f"{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    sig = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"
