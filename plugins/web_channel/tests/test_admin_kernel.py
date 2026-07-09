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


def _csrf() -> str:
    """The session-bound admin CSRF token (the groot principal is already patched)."""
    r = client.get("/admin/users")
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, "admin page must embed the csrf token"
    return m.group(1)


# --- ManageUser --------------------------------------------------------------


def test_admin_page_renders_kernel_users_table_and_kebab_menus(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    _as_groot(monkeypatch)

    async def fake_kernel_list(assertion, include_deleted=False, limit=0):
        assert assertion.count(".") == 2
        return core_pb2.ListUsersResponse(
            status=core_pb2.CALL_OK,
            users=[
                core_pb2.UserView(
                    id="12345678-aaaa-bbbb-cccc-123456789abc",
                    login="alice",
                    first_name="Alice",
                    last_name="Admin",
                    status="active",
                    roles=["admin"],
                    groups=["confluence"],
                    providers=["local"],
                    last_login_at="2026-07-08T10:00:00Z",
                )
            ],
        )

    monkeypatch.setattr(core_client, "list_users", fake_kernel_list)
    r = client.get("/admin/users")
    assert r.status_code == 200
    assert "kernel truth" in r.text
    assert 'id="admin-user-search"' in r.text
    assert "Filter the loaded list by login, name, or UUID" in r.text
    assert "Server-side search and pagination arrive with the kernel update" in r.text
    assert "alice" in r.text
    assert 'href="/admin/users/12345678-aaaa-bbbb-cccc-123456789abc"' in r.text
    assert 'title="12345678-aaaa-bbbb-cccc-123456789abc"' in r.text
    assert 'value="12345678-aaaa-bbbb-cccc-123456789abc"' in r.text
    assert 'role="menuitem"' in r.text
    assert 'value="deactivate"' in r.text
    assert 'value="delete"' in r.text
    assert 'value="grant_role"' in r.text
    assert 'value="grant_group"' in r.text
    assert 'name="target_user_id" type="text"' not in r.text


def test_admin_user_detail_renders_identity_and_actions(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    _as_groot(monkeypatch)
    user_id = "12345678-aaaa-bbbb-cccc-123456789abc"

    async def fake_kernel_list(assertion, include_deleted=False, limit=0):
        assert assertion.count(".") == 2
        return core_pb2.ListUsersResponse(
            status=core_pb2.CALL_OK,
            users=[
                core_pb2.UserView(
                    id=user_id,
                    login="alice",
                    first_name="Alice",
                    last_name="Admin",
                    nickname="aa",
                    status="active",
                    roles=["admin"],
                    groups=["confluence"],
                    providers=["local"],
                    last_login_at="2026-07-08T10:00:00Z",
                )
            ],
        )

    monkeypatch.setattr(core_client, "list_users", fake_kernel_list)
    r = client.get(f"/admin/users/{user_id}")
    assert r.status_code == 200
    assert "alice" in r.text
    assert "Alice Admin" in r.text
    assert "active" in r.text
    assert "admin" in r.text
    assert "confluence" in r.text
    assert "local" in r.text
    assert "2026-07-08T10:00:00Z" in r.text
    assert f'<code class="hkey">{user_id}</code>' in r.text
    assert 'action="/admin/kernel/user"' in r.text
    assert 'action="/admin/kernel/access"' in r.text
    assert f'name="target_user_id" value="{user_id}"' in r.text
    assert 'value="deactivate"' in r.text
    assert 'value="delete"' in r.text
    assert 'value="grant_role"' in r.text
    assert 'value="revoke_role"' in r.text
    assert 'value="grant_group"' in r.text
    assert 'value="revoke_group"' in r.text
    assert "Knowledge" in r.text and "(planned)" in r.text
    assert "LDAP" in r.text and "Confluence" in r.text


def test_admin_user_detail_unknown_id_is_honest_404(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_kernel_list(assertion, include_deleted=False, limit=0):
        return core_pb2.ListUsersResponse(
            status=core_pb2.CALL_OK,
            users=[core_pb2.UserView(id="known-id", login="alice")],
        )

    monkeypatch.setattr(core_client, "list_users", fake_kernel_list)
    r = client.get("/admin/users/missing-id")
    assert r.status_code == 404
    assert "user not found" in r.text
    assert "Could not render this kernel user detail right now." in r.text


def test_admin_user_detail_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("detail page must not list users for a non-groot")

    monkeypatch.setattr(core_client, "list_users", must_not_call)
    for user_id in ["known-id", "missing-id"]:
        r = client.get(f"/admin/users/{user_id}")
        assert r.status_code == 403


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
