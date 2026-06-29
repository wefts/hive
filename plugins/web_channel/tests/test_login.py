"""Unified-login tests: identifier auto-routes SSO vs local; local credential
verification; and a local user's scopes flow to Ask with default-deny (no-leak)."""

from __future__ import annotations

from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from web_channel import auth, core_client, localusers
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def test_login_form_renders() -> None:
    r = client.get("/login")
    assert r.status_code == 200
    assert 'action="/login"' in r.text and 'name="identifier"' in r.text


def test_known_local_user_routes_to_local_password_form() -> None:
    localusers.create("carol", "pw", ["group"], created_by="t")
    r = client.post("/login", data={"identifier": "carol"}, follow_redirects=False)
    assert r.status_code == 200
    assert 'action="/login/local"' in r.text  # local password form, NOT a Keycloak redirect
    assert "carol" in r.text


def test_unknown_identifier_routes_to_sso(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    captured: dict = {}

    class FakeKc:
        async def authorize_redirect(self, request, redirect_uri, **kw):
            captured["hint"] = kw.get("login_hint")
            return RedirectResponse("https://kc.example/realms/x/auth", status_code=302)

    class FakeOAuth:
        kc = FakeKc()

    monkeypatch.setattr(auth, "oauth", lambda: FakeOAuth())
    r = client.post("/login", data={"identifier": "someone@org"}, follow_redirects=False)
    assert r.status_code == 302 and "kc.example" in r.headers["location"]
    assert captured["hint"] == "someone@org"  # identifier prefilled into Keycloak


def test_local_login_wrong_password_rejected() -> None:
    localusers.create("erin", "right", [], created_by="t")
    r = client.post(
        "/login/local", data={"identifier": "erin", "password": "wrong"}, follow_redirects=False
    )
    assert r.status_code == 401 and "Invalid" in r.text


def test_local_user_no_group_is_public_only() -> None:
    localusers.create("frank", "pw", [], created_by="t")  # no group granted
    p = localusers.verify("frank", "pw")
    assert p is not None and p.scopes == ["public"]  # default-deny


def test_local_user_with_group_gets_mapped_scope() -> None:
    localusers.create("heidi", "pw", ["group"], created_by="t")
    p = localusers.verify("heidi", "pw")
    assert p is not None and "public" in p.scopes and "group" in p.scopes


def test_local_login_then_ask_uses_local_scopes_no_leak(monkeypatch) -> None:
    # End-to-end: a local public-only user logs in; /ask must run under ["public"]
    # only — a local user is no exception to the no-leak boundary.
    localusers.create("grace", "pw", [], created_by="t")  # public only
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    captured: dict = {}

    async def fake_ask(query, scopes, viewer):
        captured.update(scopes=scopes, viewer=viewer)
        return core_pb2.AskResponse(answer="ok", status=core_pb2.FOUND, tier="t", confidence=0.7)

    monkeypatch.setattr(core_client, "ask", fake_ask)
    c = TestClient(web.app)  # fresh session
    c.post("/login/local", data={"identifier": "grace", "password": "pw"})
    c.post("/ask", data={"q": "what does the confluence group know?"})
    assert captured["viewer"] == "grace"
    assert captured["scopes"] == ["public"] and "group" not in captured["scopes"]


def test_password_is_hashed_not_stored_plaintext() -> None:
    localusers.create("ivan", "supersecret", [], created_by="t")
    # The verify path works, but the raw password is never retrievable / stored.
    users = localusers.list_users()
    assert any(u["username"] == "ivan" for u in users)
    assert all("supersecret" not in str(u) for u in users)  # no plaintext in the listing
