"""Integration checks against a LIVE local Keycloak (excluded by default — run with
`uv run pytest -m integration` after `docker compose up -d keycloak`).

Proves the realm + real tokens map to the right scopes through auth.py, and that the
groot invite provisions a real user in a group. Env (matches compose, host-side):
  KEYCLOAK_ADMIN_URL=http://localhost:8081 KEYCLOAK_REALM=swarm-local
  KEYCLOAK_ADMIN_USER=admin KEYCLOAK_ADMIN_PASSWORD=admin
  GROUP_SCOPE_MAP='{"confluence":"group"}'
"""

from __future__ import annotations

import base64
import json
import os

import httpx
import pytest

from web_channel import auth, kc_admin

pytestmark = pytest.mark.integration

_KC = os.environ.get("OIDC_TEST_BASE", "http://localhost:8081")
_REALM = os.environ.get("KEYCLOAK_REALM", "swarm-local")
_SECRET = os.environ.get("WEB_CHANNEL_OIDC_SECRET", "web-channel-local-dev-secret-CHANGE-IN-PROD")
_TOKEN = f"{_KC}/realms/{_REALM}/protocol/openid-connect/token"


def _claims(id_token: str) -> dict:
    payload = id_token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


async def _id_token(username: str) -> str:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            _TOKEN,
            data={
                "grant_type": "password",
                "client_id": "web_channel",
                "client_secret": _SECRET,
                "username": username,
                "password": username,
                "scope": "openid",
            },
        )
        r.raise_for_status()
        return r.json()["id_token"]


@pytest.mark.parametrize(
    "username,expected_scopes,expected_groot",
    [
        ("alice", ["public", "group"], False),
        ("bob", ["public"], False),
        ("groot", ["public", "group"], True),
    ],
)
async def test_real_token_maps_to_principal(username, expected_scopes, expected_groot) -> None:
    principal = auth.principal_from_claims(_claims(await _id_token(username)))
    assert principal.viewer == username
    assert principal.scopes == expected_scopes
    assert principal.is_groot == expected_groot


async def test_groot_invite_provisions_user_in_group() -> None:
    uname = "carol-itest"
    existing = {u["username"] for u in await kc_admin.list_users()}
    if uname not in existing:
        await kc_admin.invite_user(uname, "TempPass123!", "confluence")
    users = {u["username"]: u for u in await kc_admin.list_users()}
    assert uname in users
    assert "confluence" in users[uname]["groups"]
