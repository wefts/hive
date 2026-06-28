"""Keycloak Admin API client for the groot invite/provision flow (P1).

Server-to-server over the INTERNAL Keycloak URL (no browser, so no issuer concern).
Used ONLY by groot-gated admin routes. Lean dev posture: authenticates with the KC
admin credentials. PROD hardening (noted on the card): use a dedicated service account
scoped to just `manage-users` instead of full admin creds.
"""

from __future__ import annotations

import os

import httpx


def _cfg() -> tuple[str, str, str, str]:
    return (
        os.environ.get("KEYCLOAK_ADMIN_URL", "http://keycloak:8080").rstrip("/"),
        os.environ.get("KEYCLOAK_REALM", "swarm-local"),
        os.environ.get("KEYCLOAK_ADMIN_USER", "admin"),
        os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin"),
    )


async def _admin_token(client: httpx.AsyncClient, base: str, user: str, pw: str) -> str:
    r = await client.post(
        f"{base}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": user,
            "password": pw,
        },
    )
    r.raise_for_status()
    return r.json()["access_token"]


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict, **kw):
    """GET that fails CLOSED — every admin call is checked (council: codex)."""
    r = await client.get(url, headers=headers, **kw)
    r.raise_for_status()
    return r.json()


async def list_users() -> list[dict]:
    """List realm users with their group names (aggregate identity info only)."""
    base, realm, user, pw = _cfg()
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await _admin_token(client, base, user, pw)
        h = {"Authorization": f"Bearer {tok}"}
        users = await _get_json(
            client, f"{base}/admin/realms/{realm}/users", h, params={"max": 100}
        )
        out: list[dict] = []
        for u in users:
            uid = u.get("id")
            groups = await _get_json(client, f"{base}/admin/realms/{realm}/users/{uid}/groups", h)
            out.append(
                {
                    "username": u.get("username"),
                    "email": u.get("email"),
                    "groups": [g.get("name") for g in groups],
                }
            )
        return out


async def invite_user(username: str, password: str, group: str | None = None) -> None:
    """Provision a realm user (temporary password) and optionally join a group.
    This is the lean local 'invite someone' — the groot-gated path that grants access.
    Fails CLOSED: every step is checked, and a missing group raises (never silently
    create a user without the intended grant)."""
    base, realm, user, pw = _cfg()
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await _admin_token(client, base, user, pw)
        h = {"Authorization": f"Bearer {tok}"}
        payload = {
            "username": username,
            "enabled": True,
            "emailVerified": True,
            "credentials": [{"type": "password", "value": password, "temporary": True}],
        }
        created = await client.post(f"{base}/admin/realms/{realm}/users", headers=h, json=payload)
        created.raise_for_status()
        uid = created.headers.get("Location", "").rstrip("/").rsplit("/", 1)[-1]
        if not uid:
            found = await _get_json(
                client,
                f"{base}/admin/realms/{realm}/users",
                h,
                params={"username": username, "exact": "true"},
            )
            uid = found[0]["id"]
        if group:
            grps = await _get_json(
                client, f"{base}/admin/realms/{realm}/groups", h, params={"search": group}
            )
            gid = next((g["id"] for g in grps if g.get("name") == group), None)
            if gid is None:
                raise ValueError(f"group not found in realm: {group}")
            r = await client.put(f"{base}/admin/realms/{realm}/users/{uid}/groups/{gid}", headers=h)
            r.raise_for_status()
