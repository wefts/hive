"""App-level P1 tests: OIDC gating, the cohort access no-leak boundary, and the
groot-only admin authz — driven through the FastAPI app with the principal and Core
client faked (no live kernel / IdP). OIDC is forced on per-test."""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_channel import auth, core_client, kc_admin
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _capture_ask(captured: dict):
    async def ask(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
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
    assert 'hx-post="/ask"' not in r.text  # no ask form until signed in


def test_index_shows_ask_form_when_authenticated(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("alice", ["public", "group"])
    )
    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-post="/ask"' in r.text
    assert "alice" in r.text  # identity shown


def test_admin_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("alice", ["public", "group"])
    )

    async def must_not_list():
        raise AssertionError("list_users must not be called for a non-groot")

    monkeypatch.setattr(kc_admin, "list_users", must_not_list)
    r = client.get("/admin")
    assert r.status_code == 403
    assert "groot" in r.text.lower()


def test_admin_forbidden_for_anonymous(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(web, "_current_principal", lambda request: None)
    r = client.get("/admin")
    assert r.status_code == 403


def test_admin_allows_groot(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )

    async def fake_list():
        return [{"username": "alice", "email": "a@x", "groups": ["confluence"]}]

    monkeypatch.setattr(kc_admin, "list_users", fake_list)
    r = client.get("/admin")
    assert r.status_code == 200
    assert "alice" in r.text


def test_invite_forbidden_for_non_groot_and_not_called(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("alice", ["public", "group"])
    )
    called = {"n": 0}

    async def must_not_invite(*a, **k):
        called["n"] += 1

    monkeypatch.setattr(kc_admin, "invite_user", must_not_invite)
    r = client.post(
        "/admin/invite",
        data={"username": "mallory", "password": "x", "group": "confluence"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    assert called["n"] == 0  # provisioning never reached for a non-groot


def test_invite_provisions_for_groot(monkeypatch) -> None:
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')  # confluence is a known group
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )
    captured: dict = {}

    async def fake_invite(username: str, password: str, group=None):
        captured.update(username=username, password=password, group=group)

    monkeypatch.setattr(kc_admin, "invite_user", fake_invite)
    r = client.post(
        "/admin/invite",
        data={"username": "carol", "password": "TempPass1", "group": "confluence"},
        follow_redirects=False,
    )
    assert r.status_code == 303  # redirect back to /admin
    assert captured == {"username": "carol", "password": "TempPass1", "group": "confluence"}


def test_invite_rejects_group_not_in_scope_map(monkeypatch) -> None:
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

    monkeypatch.setattr(kc_admin, "invite_user", must_not_invite)
    r = client.post(
        "/admin/invite",
        data={"username": "x", "password": "y", "group": "admins-of-everything"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert called["n"] == 0  # provisioning never reached for an unmapped group


def test_invite_does_not_log_password(monkeypatch, caplog) -> None:
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: _principal("groot", ["public"], is_groot=True)
    )

    async def fake_invite(username: str, password: str, group=None):
        return None

    monkeypatch.setattr(kc_admin, "invite_user", fake_invite)
    with caplog.at_level("INFO", logger="web_channel"):
        client.post(
            "/admin/invite",
            data={"username": "carol", "password": "SuperSecret123", "group": "confluence"},
            follow_redirects=False,
        )
    assert "SuperSecret123" not in caplog.text  # the audit line must not log the password


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
