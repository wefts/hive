"""Kernel-backed admin actions (ADR-16, step 6b.4): ManageUser / ManageAccess /
AdminReadConversation over the new Core RPCs. groot-gated at the channel; the
kernel is the real authority and is exercised through mocked RPCs here."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from web_channel import auth, core_client, localusers
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _principal(viewer: str = "groot", is_groot: bool = True) -> auth.Principal:
    return auth.Principal(
        viewer=viewer, scopes=["public"], groups=[], is_groot=is_groot, display=viewer
    )


def _as_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal())


def _csrf() -> str:
    """The session-bound admin CSRF token (the groot principal is already patched)."""
    r = client.get("/admin")
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, "admin page must embed the csrf token"
    return m.group(1)


# --- ManageUser --------------------------------------------------------------


def test_kernel_user_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("ManageUser must not be called for a non-groot")

    monkeypatch.setattr(core_client, "manage_user", must_not_call)
    r = client.post("/admin/kernel/user", data={"op": "invite", "login": "x"})
    assert r.status_code == 403


def test_kernel_user_invite_ok_creates_local_credential(monkeypatch) -> None:
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_manage_user(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK, user_id="u-new")

    monkeypatch.setattr(core_client, "manage_user", fake_manage_user)
    r = client.post(
        "/admin/kernel/user",
        data={"csrf": _csrf(), "op": "invite", "login": "uma", "password": "pw", "group": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["op"] == core_pb2.INVITE
    assert captured["login"] == "uma"
    assert localusers.exists("uma")  # the paired local credential was provisioned


def test_kernel_user_invite_rejected_by_kernel_shows_label(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def fake_manage_user(assertion, op, **kw):
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_NOT_AUTHORIZED)

    monkeypatch.setattr(core_client, "manage_user", fake_manage_user)
    r = client.post(
        "/admin/kernel/user",
        data={"csrf": _csrf(), "op": "invite", "login": "vic", "password": "pw"},
    )
    assert r.status_code == 409
    assert "not authorized" in r.text.lower()
    assert not localusers.exists("vic")  # local credential NOT created on kernel rejection


def test_kernel_user_bad_op_is_400(monkeypatch) -> None:
    _as_groot(monkeypatch)
    r = client.post("/admin/kernel/user", data={"csrf": _csrf(), "op": "not-a-real-op"})
    assert r.status_code == 400


def test_kernel_user_deactivate_calls_manage_user_with_target(monkeypatch) -> None:
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_manage_user(assertion, op, **kw):
        captured["op"] = op
        captured["target_user_id"] = kw.get("target_user_id")
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_user", fake_manage_user)
    r = client.post(
        "/admin/kernel/user",
        data={"csrf": _csrf(), "op": "deactivate", "target_user_id": "u-target"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["op"] == core_pb2.DEACTIVATE
    assert captured["target_user_id"] == "u-target"


# --- ManageAccess --------------------------------------------------------------


def test_kernel_access_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("ManageAccess must not be called for a non-groot")

    monkeypatch.setattr(core_client, "manage_access", must_not_call)
    r = client.post("/admin/kernel/access", data={"op": "grant_role"})
    assert r.status_code == 403


def test_kernel_access_grant_role_ok_redirects(monkeypatch) -> None:
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_manage_access(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_access", fake_manage_access)
    r = client.post(
        "/admin/kernel/access",
        data={"csrf": _csrf(), "op": "grant_role", "target_user_id": "u-1", "role": "admin"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["op"] == core_pb2.GRANT_ROLE
    assert captured["role"] == "admin"


def test_kernel_access_set_group_scopes_splits_comma_list(monkeypatch) -> None:
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_manage_access(assertion, op, **kw):
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_access", fake_manage_access)
    client.post(
        "/admin/kernel/access",
        data={
            "csrf": _csrf(),
            "op": "set_group_scopes",
            "group_id": "confluence",
            "scopes": "public, group",
        },
    )
    assert captured["scopes"] == ["public", "group"]


def test_kernel_access_bad_op_is_400(monkeypatch) -> None:
    _as_groot(monkeypatch)
    r = client.post("/admin/kernel/access", data={"csrf": _csrf(), "op": "nonsense"})
    assert r.status_code == 400


# --- AdminReadConversation (break-glass) --------------------------------------


def test_read_conversation_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("AdminReadConversation must not be called for a non-groot")

    monkeypatch.setattr(core_client, "admin_read_conversation", must_not_call)
    r = client.post(
        "/admin/kernel/read-conversation", data={"conversation_id": "c-1", "reason": "audit"}
    )
    assert r.status_code == 403


def test_read_conversation_renders_audit_banner_and_messages(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def fake_read(assertion, conversation_id, reason):
        return core_pb2.GetConversationResponse(
            status=core_pb2.CALL_OK,
            conversation=core_pb2.ConversationView(
                id=conversation_id, owner_id="u-owner", title="a support thread"
            ),
            messages=[
                core_pb2.MessageView(id="m1", role="user", body="help?", created_at="t0"),
                core_pb2.MessageView(
                    id="m2", role="assistant", body="sure.", ask_ref="ref-9", created_at="t1"
                ),
            ],
        )

    monkeypatch.setattr(core_client, "admin_read_conversation", fake_read)
    r = client.post(
        "/admin/kernel/read-conversation",
        data={
            "csrf": _csrf(),
            "conversation_id": "c-1",
            "reason": "user requested support escalation",
        },
    )
    assert r.status_code == 200
    assert "user requested support escalation" in r.text  # the audited reason is shown
    assert "a support thread" in r.text
    assert "help?" in r.text and "sure." in r.text
    assert 'href="/deliberation/ref-9"' in r.text


def test_read_conversation_not_found_is_honest_404(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def fake_read(assertion, conversation_id, reason):
        return core_pb2.GetConversationResponse(status=core_pb2.CALL_NOT_FOUND)

    monkeypatch.setattr(core_client, "admin_read_conversation", fake_read)
    r = client.post(
        "/admin/kernel/read-conversation",
        data={"csrf": _csrf(), "conversation_id": "missing", "reason": "audit"},
    )
    assert r.status_code == 404


def test_read_conversation_empty_reason_rejected_before_any_rpc(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("must not call the kernel with an empty reason")

    monkeypatch.setattr(core_client, "admin_read_conversation", must_not_call)
    r = client.post(
        "/admin/kernel/read-conversation",
        data={"csrf": _csrf(), "conversation_id": "c-1", "reason": "   "},
    )
    assert r.status_code == 400
