"""Unified-login tests: identifier auto-routes SSO vs local; local credential
verification; and a local user's scopes flow to Ask with default-deny (no-leak)."""

from __future__ import annotations

import grpc
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient
from grpc import aio

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

    async def fake_ask(query, scopes, viewer, **kwargs):
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


# --- ADR-16 D9: actor-assertion signing + the ResolveActor login gate ------


def test_login_without_actor_secret_is_unaffected(monkeypatch) -> None:
    # No SWARM_ACTOR_SECRET configured (pre-6a-rollout dev/test) ⇒ _resolve_and_gate
    # is a no-op; login behaves exactly as before (the existing dual-accept fallback).
    monkeypatch.delenv("SWARM_ACTOR_SECRET", raising=False)
    localusers.create("judy", "pw", [], created_by="t")
    r = client.post("/login/local", data={"identifier": "judy", "password": "pw"})
    assert r.status_code == 200  # followed the 303 redirect home


def test_login_signs_and_gates_on_resolve_actor_ok(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    localusers.create("karl", "pw", [], created_by="t")
    captured: dict = {}

    async def fake_resolve(assertion):
        captured["assertion"] = assertion
        return core_pb2.ResolveActorResponse(
            status=core_pb2.CALL_OK, uuid="u-karl", scopes=["public"], caps=[], login="karl"
        )

    monkeypatch.setattr(core_client, "resolve_actor", fake_resolve)
    r = client.post(
        "/login/local", data={"identifier": "karl", "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303  # signed in — NOT blocked
    # a real signed JWT crossed to ResolveActor, not the plaintext username
    assert captured["assertion"].count(".") == 2
    assert "karl" not in captured["assertion"].split(".")[1]  # sub is base64, not literal


def test_login_blocked_when_not_provisioned(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    localusers.create("liam", "pw", [], created_by="t")

    async def fake_resolve(assertion):
        return core_pb2.ResolveActorResponse(status=core_pb2.CALL_UNAUTHENTICATED)

    monkeypatch.setattr(core_client, "resolve_actor", fake_resolve)
    r = client.post(
        "/login/local", data={"identifier": "liam", "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 403
    assert "not provisioned" in r.text.lower()
    # the honest-block path must not have started a session
    assert "session" not in r.cookies or not r.cookies.get("session")


def test_login_falls_open_when_kernel_unreachable(monkeypatch) -> None:
    # A transient ResolveActor transport failure must NOT lock out a legitimate
    # user — the dual-accept legacy path self-heals once the kernel is reachable.
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    localusers.create("mia", "pw", [], created_by="t")

    async def boom(assertion):
        raise aio.AioRpcError(
            grpc.StatusCode.UNAVAILABLE, aio.Metadata(), aio.Metadata(), details="down"
        )

    monkeypatch.setattr(core_client, "resolve_actor", boom)
    r = client.post(
        "/login/local", data={"identifier": "mia", "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303  # signed in despite the kernel being unreachable


def test_login_fails_closed_on_active_kernel_rejection(monkeypatch) -> None:
    # Council fix (codex+gemini): only UNAVAILABLE/DEADLINE_EXCEEDED fail open. Any
    # other gRPC code means the kernel actively rejected/errored the call — must
    # NOT silently log the user in unverified.
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    localusers.create("oscar", "pw", [], created_by="t")

    async def boom(assertion):
        raise aio.AioRpcError(
            grpc.StatusCode.INTERNAL, aio.Metadata(), aio.Metadata(), details="boom"
        )

    monkeypatch.setattr(core_client, "resolve_actor", boom)
    r = client.post(
        "/login/local", data={"identifier": "oscar", "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 502
    assert "verification failed" in r.text.lower()


def test_ask_sends_signed_assertion_not_plaintext_viewer(monkeypatch) -> None:
    # Once signing is configured, the wire `viewer` field must carry the signed
    # assertion (never send a dotted plaintext viewer — the dual-accept footgun).
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)

    async def fake_resolve(assertion):
        return core_pb2.ResolveActorResponse(status=core_pb2.CALL_OK, uuid="u-nina", caps=[])

    monkeypatch.setattr(core_client, "resolve_actor", fake_resolve)
    localusers.create("nina", "pw", [], created_by="t")
    c = TestClient(web.app)
    c.post("/login/local", data={"identifier": "nina", "password": "pw"})

    captured: dict = {}

    async def fake_ask(query, scopes, viewer, **kwargs):
        captured["viewer"] = viewer
        return core_pb2.AskResponse(answer="ok", status=core_pb2.FOUND, tier="t", confidence=0.7)

    monkeypatch.setattr(core_client, "ask", fake_ask)
    c.post("/ask", data={"q": "anything"})
    assert captured["viewer"].count(".") == 2  # a signed JWT, not "nina"
    assert captured["viewer"] != "nina"
