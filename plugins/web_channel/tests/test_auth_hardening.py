"""Auth hardening (board/todo/web-channel-auth-hardening): per-form CSRF tokens on
the admin POST routes, and stale pre-6b sessions (no sub/provider) forced to
re-auth instead of silently degrading to anonymous under the kernel's :strict
mode. The CSRF token is session-bound (synchronizer pattern) and compared
constant-time; a forged cross-origin POST carries no (or a wrong) token and is
rejected before any kernel RPC fires."""

from __future__ import annotations

import re
from typing import Any, cast

from fastapi.testclient import TestClient

from web_channel import auth, core_client
from web_channel import main as web

client = TestClient(web.app)


def _principal(viewer: str = "groot", is_groot: bool = True) -> auth.Principal:
    return auth.Principal(
        viewer=viewer,
        scopes=["public"],
        groups=[],
        is_groot=is_groot,
        display=viewer,
        sub=viewer,
        provider="local",
    )


def _as_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal())


def _csrf_token() -> str:
    """GET /admin (hub) as groot and scrape the session-bound token from a form."""
    r = client.get("/admin")
    assert r.status_code == 200
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, "admin forms must embed the csrf token"
    return m.group(1)


# --- CSRF on the admin POST routes -------------------------------------------


def test_admin_post_without_csrf_is_rejected_and_rpc_never_fires(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("kernel RPC must not fire on a forged POST")

    monkeypatch.setattr(core_client, "manage_user", must_not_call)
    monkeypatch.setattr(core_client, "manage_access", must_not_call)
    monkeypatch.setattr(core_client, "admin_read_conversation", must_not_call)

    # no token at all (the forged cross-origin shape — an attacker page can't read it)
    for path, data in [
        ("/admin/kernel/user", {"op": "invite", "login": "x"}),
        ("/admin/kernel/access", {"op": "grant_role", "role": "admin"}),
        ("/admin/kernel/read-conversation", {"conversation_id": "c", "reason": "r"}),
        (
            "/admin/auth",
            {
                "issuer": "http://kc.test/realms/swarm",
                "realm": "swarm",
                "client_id": "web",
                "admin_url": "http://kc.test",
                "admin_user": "admin",
            },
        ),
    ]:
        r = client.post(path, data=data)
        assert r.status_code == 403, path

    assert client.post("/admin/invite", data={"username": "x", "password": "p"}).status_code == 404
    assert (
        client.post("/admin/local-invite", data={"username": "x", "password": "p"}).status_code
        == 404
    )

    # a wrong token is equally rejected
    r = client.post(
        "/admin/kernel/user",
        data={"op": "invite", "login": "x", "csrf": "forged-token"},
    )
    assert r.status_code == 403


def test_admin_post_with_session_token_passes_the_csrf_gate(monkeypatch) -> None:
    _as_groot(monkeypatch)
    token = _csrf_token()
    called: dict = {}

    async def fake_manage_access(assertion, op, **kw):
        called["op"] = op
        from web_channel._gen import core_pb2

        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_access", fake_manage_access)
    r = client.post(
        "/admin/kernel/access",
        data={"op": "grant_group", "target_user_id": "u", "group_id": "g", "csrf": token},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "op" in called


def test_admin_page_embeds_the_token_in_every_form(monkeypatch) -> None:
    _as_groot(monkeypatch)
    r = client.get("/admin")
    assert r.status_code == 200
    forms = r.text.count("<form")
    tokens = r.text.count('name="csrf"')
    assert forms > 0 and tokens == forms


# --- stale pre-6b sessions are forced to re-auth ------------------------------


class _Req:
    """A Request stand-in exposing only `.session` (all _current_principal touches)."""

    def __init__(self, session: dict) -> None:
        self.session = session


def _req(session: dict) -> Any:
    return cast(Any, _Req(session))


def test_session_without_sub_provider_is_cleared_and_forces_reauth() -> None:
    stale = {"user": {"viewer": "alice", "scopes": ["public"], "groups": []}}
    assert web._current_principal(_req(stale)) is None
    assert "user" not in stale  # cleared — the next request lands on /login


def test_complete_session_still_resolves() -> None:
    good = {
        "user": {
            "viewer": "alice",
            "scopes": ["public"],
            "groups": [],
            "sub": "sub-alice",
            "provider": "keycloak",
        }
    }
    p = web._current_principal(_req(good))
    assert p is not None and p.sub == "sub-alice"
    assert "user" in good  # untouched


# --- JIT provisioning at SSO login (ADR-16 D3) --------------------------------


def test_provision_kernel_identity_signs_and_calls_the_rpc(monkeypatch) -> None:
    import anyio

    from web_channel import core_client
    from web_channel._gen import core_pb2

    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    captured: dict = {}

    async def fake_provision(token: str):
        captured["token"] = token
        return core_pb2.ResolveActorResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "provision_actor", fake_provision)
    principal = auth.Principal(
        viewer="carol",
        scopes=["group"],
        groups=["staff"],
        display="carol",
        sub="sub-carol",
        provider="keycloak",
    )
    claims = {"given_name": "Carol", "email": "carol@example.test"}
    anyio.run(web._provision_kernel_identity, claims, principal)

    import base64
    import json as _json

    seg = captured["token"].split(".")[1]
    payload = _json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
    assert payload["aud"] == "swarm.provision.v1"
    assert payload["sub"] == "sub-carol"
    assert payload["login"] == "carol"
    assert payload["groups"] == ["staff"]
    assert payload["first_name"] == "Carol"


def test_provision_kernel_identity_is_noop_without_secret(monkeypatch) -> None:
    import anyio

    from web_channel import core_client

    monkeypatch.delenv("SWARM_ACTOR_SECRET", raising=False)

    async def must_not_call(token: str):
        raise AssertionError("ProvisionActor must not fire without a signing secret")

    monkeypatch.setattr(core_client, "provision_actor", must_not_call)
    principal = auth.Principal(
        viewer="carol", scopes=[], groups=[], display="carol", sub="s", provider="keycloak"
    )
    anyio.run(web._provision_kernel_identity, {}, principal)


# --- RP-initiated logout (the Keycloak SSO session must die too) ---------------


def test_logout_redirects_to_keycloak_end_session(monkeypatch) -> None:
    """Channel-only logout left the Keycloak SSO cookie alive: the next /login
    silently re-authenticated the SAME user (observed live: every callback carried
    the same session_state — bob could never log in after alice). /logout must
    end the IdP session too."""
    monkeypatch.setenv("OIDC_ENABLED", "true")
    monkeypatch.setenv("OIDC_ISSUER", "http://kc.test:8081/realms/swarm-local")
    monkeypatch.setenv("OIDC_CLIENT_ID", "swarm-web")

    with TestClient(web.app) as c:
        # seed a session carrying an id_token (as the OIDC callback now stores)
        c.get("/healthz")  # boot
        # simulate the session by hitting logout with a crafted session cookie is
        # awkward; instead call the URL builder directly + the route without session
        url = web._end_session_url({"id_token": "idtok-123"}, "http://app.test")
        assert url.startswith(
            "http://kc.test:8081/realms/swarm-local/protocol/openid-connect/logout?"
        )
        assert "id_token_hint=idtok-123" in url
        assert "post_logout_redirect_uri=http%3A%2F%2Fapp.test" in url
        assert "client_id=swarm-web" in url

        r = c.get("/logout", follow_redirects=False)
        # no session/id_token → still redirects to Keycloak (client_id flow) so the
        # SSO cookie dies even for a stale channel session
        assert r.status_code == 307
        assert "/protocol/openid-connect/logout" in r.headers["location"]


def test_logout_stays_local_when_oidc_off(monkeypatch) -> None:
    monkeypatch.setenv("OIDC_ENABLED", "false")
    with TestClient(web.app) as c:
        r = c.get("/logout", follow_redirects=False)
        assert r.status_code == 307
        assert r.headers["location"] == "/"
