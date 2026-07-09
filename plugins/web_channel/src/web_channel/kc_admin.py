"""Keycloak Admin API client for the groot invite/provision flow (P1).

Server-to-server over the INTERNAL Keycloak URL (no browser, so no issuer concern).
Used ONLY by groot-gated admin routes. Lean dev posture: authenticates with the KC
admin credentials. PROD hardening (noted on the card): use a dedicated service account
scoped to just `manage-users` instead of full admin creds.
"""

from __future__ import annotations

import httpx

from web_channel import settings


def _cfg() -> tuple[str, str, str, str]:
    return (
        settings.get_or_env("KEYCLOAK_ADMIN_URL", "http://keycloak:8080").rstrip("/"),
        settings.get_or_env("KEYCLOAK_REALM", "swarm-local"),
        settings.get_or_env("KEYCLOAK_ADMIN_USER", "admin"),
        settings.get_or_env("KEYCLOAK_ADMIN_PASSWORD", "admin"),
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


async def check_connector(
    issuer: str,
    admin_url: str,
    admin_user: str,
    admin_password: str,
    timeout: float = 3,
) -> dict[str, bool]:
    """Connectivity probe for operator settings; returns only booleans, never secrets."""
    out = {"oidc": False, "admin": False}
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.get(f"{issuer.rstrip('/')}/.well-known/openid-configuration")
            r.raise_for_status()
            out["oidc"] = True
        except Exception:
            out["oidc"] = False
        try:
            await _admin_token(client, admin_url.rstrip("/"), admin_user, admin_password)
            out["admin"] = True
        except Exception:
            out["admin"] = False
    return out
