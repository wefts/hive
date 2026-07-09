"""App-level P1 tests: OIDC gating, the cohort access no-leak boundary, and the
groot-only admin authz — driven through the FastAPI app with the principal and Core
client faked (no live kernel / IdP). OIDC is forced on per-test."""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_channel import auth, core_client, kc_admin
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _p1_csrf() -> str:
    import re as _re

    r = client.get("/admin/users")
    m = _re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, "admin page must embed the csrf token"
    return m.group(1)


def _capture_ask(captured: dict):
    async def ask(query: str, scopes: list[str], viewer: str, **kwargs) -> core_pb2.AskResponse:
        captured.update(query=query, scopes=scopes, viewer=viewer)
        return core_pb2.AskResponse(answer="ok", confidence=0.7, tier="t", status=core_pb2.FOUND)

    return ask


def _principal(viewer: str, scopes: list[str], is_groot: bool = False) -> auth.Principal:
    return auth.Principal(
        viewer=viewer, scopes=scopes, groups=[], is_groot=is_groot, display=viewer
    )


def test_ask_requires_login_when_oidc_on(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(web, "_current_principal", lambda request: None)
    called = {"n": 0}

    async def must_not_call(*a, **k):
        called["n"] += 1
        return core_pb2.AskResponse()

    monkeypatch.setattr(core_client, "ask", must_not_call)
    r = client.post("/ask", data={"q": "secret?"})
    assert r.status_code == 200  # no crash
    assert "sign in" in r.text.lower() and "/login" in r.text
    assert called["n"] == 0  # never query the kernel anonymously


def test_ask_uses_authenticated_scopes_alice(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("alice", ["public", "group"])
    )
    captured: dict = {}
    monkeypatch.setattr(core_client, "ask", _capture_ask(captured))
    client.post("/ask", data={"q": "group question"})
    assert captured["viewer"] == "alice"
    assert captured["scopes"] == ["public", "group"]


def test_no_leak_bob_scopes_exclude_group(monkeypatch) -> None:
    # The cohort no-leak boundary: a user without the group must NOT have its scope
    # sent to the kernel — so the kernel can never return group content to bob.
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal("bob", ["public"]))
    captured: dict = {}
    monkeypatch.setattr(core_client, "ask", _capture_ask(captured))
    client.post("/ask", data={"q": "what does the confluence group know?"})
    assert captured["viewer"] == "bob"
    assert captured["scopes"] == ["public"]
    assert "group" not in captured["scopes"]


def test_index_shows_login_when_anonymous(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(web, "_current_principal", lambda request: None)
    r = client.get("/")
    assert r.status_code == 200
    assert "/login" in r.text
    assert 'hx-post="/ask' not in r.text  # no ask form (neither phase) until signed in


def test_index_shows_ask_form_when_authenticated(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("alice", ["public", "group"])
    )
    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-post="/ask/start"' in r.text
    assert "alice" in r.text  # identity shown


def test_admin_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("alice", ["public", "group"])
    )

    async def must_not_list(*args, **kwargs):
        raise AssertionError("list_users must not be called for a non-groot")

    monkeypatch.setattr(kc_admin, "list_users", must_not_list)
    monkeypatch.setattr(core_client, "list_users", must_not_list)
    for path in [
        "/admin",
        "/admin/users",
        "/admin/users/known-id",
        "/admin/auth",
        "/admin/connector",
        "/admin/connectors",
        "/admin/tools",
    ]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 403, path
        assert "forbidden" in r.text.lower()


def test_admin_forbidden_for_anonymous(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(web, "_current_principal", lambda request: None)
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 403


def test_admin_hub_renders_for_groot(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    # A signable identity (sub+provider) so the hub can mint the actor assertion
    # ListUsers needs — a bare _principal() can't sign, so the count would stay blank.
    signed_groot = _principal("groot", ["public"], is_groot=True)
    signed_groot.sub, signed_groot.provider = "groot-sub", "keycloak"
    monkeypatch.setattr(web, "_current_principal", lambda request: signed_groot)
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)

    async def fake_kernel_list(assertion, include_deleted=False, limit=0, query="", offset=0):
        assert assertion.count(".") == 2
        return core_pb2.ListUsersResponse(
            status=core_pb2.CALL_OK,
            total=1,
            users=[core_pb2.UserView(id="u-1", login="alice")],
        )

    monkeypatch.setattr(core_client, "list_users", fake_kernel_list)
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 200
    assert 'action="/admin/kernel/user"' in r.text
    assert 'value="invite"' in r.text
    assert "jit-provision-rpc" in r.text
    assert 'href="/admin/users"' in r.text
    assert 'href="/admin/auth"' in r.text
    assert 'href="/admin/connectors"' in r.text
    assert 'href="/admin/tools"' in r.text
    assert 'href="/admin/groups"' in r.text
    assert 'href="/admin/roles"' in r.text
    assert "1</span> kernel users" in r.text


def test_auth_provider_page_allows_groot_and_lists_realm_users(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )

    async def fake_list():
        return [{"username": "alice", "email": "a@x", "groups": ["confluence"]}]

    monkeypatch.setattr(kc_admin, "list_users", fake_list)
    r = client.get("/admin/auth")
    assert r.status_code == 200
    assert "Auth Provider (Keycloak / OIDC)" in r.text
    assert 'action="/admin/auth"' in r.text
    assert 'hx-get="/admin/auth/status"' in r.text
    assert "Keycloak realm users" in r.text
    assert "alice" in r.text


def test_admin_connector_legacy_redirects_to_auth(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )
    r = client.get("/admin/connector", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/auth"


def test_connectors_page_is_honest_placeholder_without_backend_calls(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )

    async def must_not_call(*args, **kwargs):
        raise AssertionError("connectors placeholder must not call backends")

    monkeypatch.setattr(kc_admin, "list_users", must_not_call)
    monkeypatch.setattr(core_client, "list_users", must_not_call)
    r = client.get("/admin/connectors")
    assert r.status_code == 200
    assert "Connectors" in r.text
    assert "planned" in r.text
    assert "knowledge ingest sources" in r.text
    assert "default-deny" in r.text


def test_admin_nav_shows_redesigned_sections(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert "Users" in r.text
    assert 'href="/admin/groups"' in r.text
    assert 'href="/admin/roles"' in r.text
    assert "Auth Provider" in r.text
    assert "Connectors" in r.text
    assert "Tools" in r.text


def test_users_page_does_not_call_keycloak(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )

    async def must_not_list():
        raise AssertionError("users page must not call Keycloak")

    async def fake_kernel_list(assertion, include_deleted=False, limit=0, query="", offset=0):
        return core_pb2.ListUsersResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(kc_admin, "list_users", must_not_list)
    monkeypatch.setattr(core_client, "list_users", fake_kernel_list)
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert "Keycloak realm users" not in r.text


def test_tools_page_contains_break_glass(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )
    r = client.get("/admin/tools")
    assert r.status_code == 200
    assert "Break-glass conversation read" in r.text
    assert 'action="/admin/kernel/read-conversation"' in r.text


def test_deleted_invite_routes_are_gone() -> None:
    r = client.post(
        "/admin/invite",
        data={"username": "mallory", "password": "x", "group": "confluence"},
        follow_redirects=False,
    )
    assert r.status_code == 404
    r = client.post(
        "/admin/local-invite",
        data={"username": "mallory", "password": "x", "group": "confluence"},
        follow_redirects=False,
    )
    assert r.status_code == 404


def test_kernel_invite_forbidden_for_non_groot_and_not_called(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("alice", ["public", "group"])
    )
    called = {"n": 0}

    async def must_not_invite(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(core_client, "manage_user", must_not_invite)
    r = client.post(
        "/admin/kernel/user",
        data={"op": "invite", "login": "mallory", "password": "x", "group": "confluence"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert called["n"] == 0  # provisioning never reached for a non-groot


def test_kernel_invite_provisions_for_groot(monkeypatch) -> None:
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')  # confluence is a known group
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )
    captured: dict = {}

    async def fake_manage_user(assertion, op, **kw):
        captured.update(op=op, **kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_user", fake_manage_user)
    r = client.post(
        "/admin/kernel/user",
        data={
            "csrf": _p1_csrf(),
            "op": "invite",
            "login": "carol",
            "password": "TempPass1",
            "group": "confluence",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/users"
    assert captured["op"] == core_pb2.INVITE
    assert captured["login"] == "carol"


def test_kernel_invite_rejects_group_not_in_scope_map(monkeypatch) -> None:
    # groot may only assign groups the channel maps to a scope — never an arbitrary
    # Keycloak group (council: codex).
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )
    called = {"n": 0}

    async def must_not_invite(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(core_client, "manage_user", must_not_invite)
    r = client.post(
        "/admin/kernel/user",
        data={
            "csrf": _p1_csrf(),
            "op": "invite",
            "login": "x",
            "password": "y",
            "group": "admins-of-everything",
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert called["n"] == 0


def test_kernel_invite_does_not_log_password(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )

    async def fake_manage_user(assertion, op, **kw):
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_user", fake_manage_user)
    with caplog.at_level("INFO", logger="web_channel"):
        client.post(
            "/admin/kernel/user",
            data={
                "csrf": _p1_csrf(),
                "op": "invite",
                "login": "carol",
                "password": "SuperSecret123",
                "group": "confluence",
            },
            follow_redirects=False,
        )
    assert "SuperSecret123" not in caplog.text


def test_connector_test_before_save_failure_persists_nothing(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )

    async def fail_check(values):
        return {"oidc": True, "admin": False}

    monkeypatch.setattr(web, "_connector_check", fail_check)
    from web_channel import settings

    r = client.post(
        "/admin/auth",
        data={
            "csrf": _p1_csrf(),
            "issuer": "http://kc.test/realms/swarm",
            "realm": "swarm",
            "client_id": "web",
            "client_secret": "new-secret",
            "admin_url": "http://kc-admin.test",
            "admin_user": "admin",
            "admin_password": "new-admin-secret",
        },
    )
    assert r.status_code == 400
    assert settings.get("OIDC_ISSUER") is None
    assert settings.get("OIDC_CLIENT_SECRET") is None


def test_connector_page_never_renders_secret_values(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )
    from web_channel import settings

    settings.put("OIDC_CLIENT_SECRET", "client-secret-value")
    settings.put("KEYCLOAK_ADMIN_PASSWORD", "admin-secret-value")
    r = client.get("/admin/auth")
    assert r.status_code == 200
    assert "client-secret-value" not in r.text
    assert "admin-secret-value" not in r.text


def test_session_secret_never_a_committed_default(monkeypatch) -> None:
    # A known signing key would let anyone forge is_groot/scopes — so unset/placeholder
    # must yield a fresh random key, never a committed constant (council: all 3 reviewers).
    for placeholder in ("", "dev-insecure-session-secret", "dev-session-secret-CHANGE-IN-PROD"):
        monkeypatch.setenv("SESSION_SECRET", placeholder)
        s = web._session_secret()
        assert s not in web._PLACEHOLDER_SECRETS
        assert len(s) >= 20
    monkeypatch.setenv("SESSION_SECRET", "a-real-strong-secret-value-set-by-operator")
    assert web._session_secret() == "a-real-strong-secret-value-set-by-operator"
