"""Kernel-backed admin actions (ADR-16 step 6b.4, ADR-20): ManageUser / ManageAccess /
ManageProject / Elevate / AdminReadConversation over the Core RPCs. The channel gates the
console on kernel-derived caps (`is_admin`; elevation-only sections on `is_elevated`); the
kernel is the real authority and is exercised through mocked RPCs here."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from web_channel import auth, core_client, localusers
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _principal(
    viewer: str = "groot", is_admin: bool = True, is_elevated: bool = False
) -> auth.Principal:
    return auth.Principal(
        viewer=viewer,
        scopes=["public"],
        groups=[],
        is_admin=is_admin,
        is_elevated=is_elevated,
        elevation_expires_at="2026-08-27T23:59:00Z" if is_elevated else "",
        display=viewer,
        sub=viewer,
        provider="local",
        sid="sess-1",
    )


def _as_groot(monkeypatch) -> None:
    """A plain ADMIN (any admin cap; NOT elevated)."""
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal())


def _as_elevated(monkeypatch) -> None:
    """An admin holding a LIVE elevation for this session (superadmin caps)."""
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_elevated=True))


def _csrf() -> str:
    """The session-bound admin CSRF token (the groot principal is already patched)."""
    r = client.get("/admin")
    m = re.search(r'name="csrf" value="([^"]+)"', r.text)
    assert m, "admin page must embed the csrf token"
    return m.group(1)


# --- ManageUser --------------------------------------------------------------


def test_admin_users_roster_is_slim_and_links_to_detail(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)
    uid = "12345678-aaaa-bbbb-cccc-123456789abc"

    async def fake_kernel_list(assertion, include_deleted=False, limit=0, query="", offset=0):
        assert assertion.count(".") == 2
        return core_pb2.ListUsersResponse(
            status=core_pb2.CALL_OK,
            total=1,
            users=[
                core_pb2.UserView(
                    id=uid,
                    login="alice",
                    first_name="Alice",
                    last_name="Admin",
                    status="active",
                    roles=["admin"],
                    groups=["everyone"],
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
    assert 'hx-get="/admin/users/roster"' in r.text
    assert "alice" in r.text
    # the login IS the sole link to the detail page — no verb link, no kebab, no UUID col
    assert f'href="/admin/users/{uid}"' in r.text
    assert "Manage →" not in r.text
    assert 'role="menuitem"' not in r.text
    assert "grant_role" not in r.text and "grant_group" not in r.text
    assert 'value="delete"' not in r.text
    # the truncated-UUID column (phantom duplicates) is gone from the roster
    assert f'title="{uid}"' not in r.text
    assert ">UUID<" not in r.text


def test_admin_user_detail_renders_identity_and_actions(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)
    user_id = "12345678-aaaa-bbbb-cccc-123456789abc"

    async def fake_get_user(assertion, uid):
        assert assertion.count(".") == 2
        assert uid == user_id
        return core_pb2.GetUserResponse(
            status=core_pb2.CALL_OK,
            user=core_pb2.UserView(
                id=user_id,
                login="alice",
                first_name="Alice",
                last_name="Admin",
                nickname="aa",
                status="active",
                roles=["admin"],
                groups=["admins", "staff"],
                providers=["local"],
                last_login_at="2026-07-08T10:00:00Z",
                emails=["alice@example.org"],
                projects=["p-1"],
            ),
        )

    async def fake_list_projects(assertion, mine_only=False):
        return core_pb2.ListProjectsResponse(
            status=core_pb2.CALL_OK,
            projects=[core_pb2.ProjectView(id="p-1", name="Internal", visibility="shared")],
        )

    monkeypatch.setattr(core_client, "get_user", fake_get_user)
    monkeypatch.setattr(core_client, "list_projects", fake_list_projects)
    r = client.get(f"/admin/users/{user_id}")
    assert r.status_code == 200
    assert "alice" in r.text
    assert "Alice Admin" in r.text
    assert "active" in r.text
    assert "alice@example.org" in r.text  # emails — GetUser-only PII
    assert f'<code class="hkey">{user_id}</code>' in r.text  # full UUID on the card
    # group membership (not per-user roles): grant/revoke GROUP only, never a role
    assert 'action="/admin/kernel/access"' in r.text
    assert f'name="target_user_id" value="{user_id}"' in r.text
    assert "grant_role" not in r.text and "revoke_role" not in r.text
    assert 'value="revoke_group"' in r.text  # alice ∈ admins/staff → Remove
    assert 'value="admins"' in r.text and 'value="staff"' in r.text
    # wheel is elevation-managed: no form for a plain admin, an honest note instead
    assert 'value="wheel"' not in r.text
    assert "requires an elevation" in r.text
    # the retired groups are gone
    assert "everyone" not in r.text and "superuser" not in r.text
    # lifecycle on the detail page
    assert 'action="/admin/kernel/user"' in r.text
    assert 'value="deactivate"' in r.text and 'value="delete"' in r.text
    # Projects the user can see (the sharing object), linking to the project page
    assert 'href="/admin/projects/p-1"' in r.text and "Internal" in r.text
    assert "Local channel credential" in r.text


def test_admin_user_detail_offers_wheel_membership_only_when_elevated(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_elevated(monkeypatch)
    user_id = "12345678-aaaa-bbbb-cccc-123456789abc"

    async def fake_get_user(assertion, uid):
        return core_pb2.GetUserResponse(
            status=core_pb2.CALL_OK,
            user=core_pb2.UserView(
                id=user_id, login="loc", status="active", groups=["staff"], providers=["local"]
            ),
        )

    async def fake_list_projects(assertion, mine_only=False):
        return core_pb2.ListProjectsResponse(status=core_pb2.CALL_OK, projects=[])

    monkeypatch.setattr(core_client, "get_user", fake_get_user)
    monkeypatch.setattr(core_client, "list_projects", fake_list_projects)
    r = client.get(f"/admin/users/{user_id}")
    assert r.status_code == 200
    assert 'value="wheel"' in r.text and 'value="grant_group"' in r.text


def test_admin_user_detail_unknown_id_is_honest_404(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_get_user(assertion, uid):
        return core_pb2.GetUserResponse(status=core_pb2.CALL_NOT_FOUND)

    monkeypatch.setattr(core_client, "get_user", fake_get_user)
    r = client.get("/admin/users/missing-id")
    assert r.status_code == 404
    assert "user not found" in r.text
    assert "Could not render this kernel user detail right now." in r.text


def test_admin_user_detail_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("detail page must not fetch a user for a non-groot")

    monkeypatch.setattr(core_client, "get_user", must_not_call)
    for user_id in ["known-id", "missing-id"]:
        r = client.get(f"/admin/users/{user_id}")
        assert r.status_code == 403


def test_kernel_user_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))

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
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("ManageAccess must not be called for a non-groot")

    monkeypatch.setattr(core_client, "manage_access", must_not_call)
    r = client.post("/admin/kernel/access", data={"op": "grant_role"})
    assert r.status_code == 403


def test_kernel_access_role_and_scope_ops_are_gone(monkeypatch) -> None:
    # ADR-19/20: no per-user role grants, no group scope grants — the channel does not offer
    # them and never forwards them (the kernel would answer BAD_REQUEST anyway).
    _as_groot(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("retired op must not reach the kernel")

    monkeypatch.setattr(core_client, "manage_access", must_not_call)
    for op in ("grant_role", "revoke_role", "set_group_scopes"):
        r = client.post(
            "/admin/kernel/access",
            data={"csrf": _csrf(), "op": op, "target_user_id": "u-1", "role": "admin"},
        )
        assert r.status_code == 400


def test_kernel_access_grant_group_forwards_group_membership(monkeypatch) -> None:
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_manage_access(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_access", fake_manage_access)
    r = client.post(
        "/admin/kernel/access",
        data={"csrf": _csrf(), "op": "grant_group", "target_user_id": "u-1", "group_id": "staff"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["op"] == core_pb2.GRANT_GROUP
    assert captured["group_id"] == "staff"
    assert "role" not in captured and "scopes" not in captured


def test_kernel_access_bad_op_is_400(monkeypatch) -> None:
    _as_groot(monkeypatch)
    r = client.post("/admin/kernel/access", data={"csrf": _csrf(), "op": "nonsense"})
    assert r.status_code == 400


# --- AdminReadConversation (break-glass) --------------------------------------


def test_read_conversation_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("AdminReadConversation must not be called for a non-groot")

    monkeypatch.setattr(core_client, "admin_read_conversation", must_not_call)
    r = client.post(
        "/admin/kernel/read-conversation", data={"conversation_id": "c-1", "reason": "audit"}
    )
    assert r.status_code == 403


def test_read_conversation_renders_audit_banner_and_messages(monkeypatch) -> None:
    _as_elevated(monkeypatch)

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
    _as_elevated(monkeypatch)

    async def fake_read(assertion, conversation_id, reason):
        return core_pb2.GetConversationResponse(status=core_pb2.CALL_NOT_FOUND)

    monkeypatch.setattr(core_client, "admin_read_conversation", fake_read)
    r = client.post(
        "/admin/kernel/read-conversation",
        data={"csrf": _csrf(), "conversation_id": "missing", "reason": "audit"},
    )
    assert r.status_code == 404


def test_read_conversation_empty_reason_rejected_before_any_rpc(monkeypatch) -> None:
    _as_elevated(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("must not call the kernel with an empty reason")

    monkeypatch.setattr(core_client, "admin_read_conversation", must_not_call)
    r = client.post(
        "/admin/kernel/read-conversation",
        data={"csrf": _csrf(), "conversation_id": "c-1", "reason": "   "},
    )
    assert r.status_code == 400


def test_read_conversation_needs_an_elevation_at_the_channel_gate(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("a non-elevated admin must not reach break-glass")

    monkeypatch.setattr(core_client, "admin_read_conversation", must_not_call)
    r = client.post(
        "/admin/kernel/read-conversation",
        data={"csrf": _csrf(), "conversation_id": "c-1", "reason": "peek"},
    )
    assert r.status_code == 403
    # and the Tools page shows the honest gate instead of the form
    page = client.get("/admin/tools")
    assert "elevation required" in page.text.lower()
    assert 'action="/admin/kernel/read-conversation"' not in page.text


# --- Groups & Roles (fixed set: ListGroups / ListRoles / ManageGroup role binding) ----


def test_admin_groups_page_lists_the_fixed_three_without_scopes(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_list_groups(assertion):
        assert assertion.count(".") == 2
        return core_pb2.ListGroupsResponse(
            status=core_pb2.CALL_OK,
            groups=[
                core_pb2.GroupView(
                    id="admins", name="Admins", member_count=1, granted_roles=["admin"]
                ),
                core_pb2.GroupView(id="staff", name="Staff", member_count=9),
                core_pb2.GroupView(id="wheel", name="Wheel", member_count=1),
            ],
        )

    monkeypatch.setattr(core_client, "list_groups", fake_list_groups)
    r = client.get("/admin/groups")
    assert r.status_code == 200
    assert "Wheel" in r.text and "Admins" in r.text and "Staff" in r.text
    assert 'href="/admin/groups/wheel"' in r.text
    # no lifecycle, no scope picker, no baseline: groups grant NO visibility
    for gone in (
        'value="rename"',
        'value="delete"',
        'value="set_scopes"',
        "New group",
        "Baseline",
        "src:",
        "baseline",
        "Everyone",
        "Superuser",
    ):
        assert gone not in r.text, gone
    assert "never grant visibility" in r.text
    assert "elevation" in r.text  # wheel is elevation-managed


def test_admin_roles_page_is_read_only(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_list_roles(assertion):
        return core_pb2.ListRolesResponse(
            status=core_pb2.CALL_OK,
            roles=[
                core_pb2.RoleView(
                    name="admin", capabilities=["manage_access", "invite_users"], holder_count=2
                ),
                core_pb2.RoleView(name="superadmin", capabilities=["all"], holder_count=1),
            ],
        )

    monkeypatch.setattr(core_client, "list_roles", fake_list_roles)
    r = client.get("/admin/roles")
    assert r.status_code == 200
    assert "admin" in r.text and "superadmin" in r.text
    assert "manage_access" in r.text
    assert "read-only" in r.text
    # no mutation affordance on the roles page
    assert 'action="/admin/kernel/group"' not in r.text
    assert 'action="/admin/kernel/access"' not in r.text


def test_kernel_group_role_binding_is_elevation_only_and_forwards_set_role(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    # a plain admin is refused at the channel gate (the kernel would too)
    _as_groot(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("role binding must not reach the kernel from a plain admin")

    monkeypatch.setattr(core_client, "manage_group", must_not_call)
    r = client.post(
        "/admin/kernel/group",
        data={"csrf": _csrf(), "op": "set_role", "group_id": "staff", "role": "admin"},
    )
    assert r.status_code == 403

    _as_elevated(monkeypatch)
    captured: dict = {}

    async def fake_manage_group(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_group", fake_manage_group)
    r = client.post(
        "/admin/kernel/group",
        data={"csrf": _csrf(), "op": "set_role", "group_id": "staff", "role": "admin"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/groups/staff"
    assert captured["op"] == core_pb2.GROUP_SET_ROLE
    assert captured["group_id"] == "staff" and captured["role"] == "admin"
    assert "scopes" not in captured
    # the fixed set has no lifecycle: create/delete/set_scopes are not offered (400)
    for op in ("create", "delete", "set_scopes", "rename"):
        assert (
            client.post(
                "/admin/kernel/group", data={"csrf": _csrf(), "op": op, "group_id": "x"}
            ).status_code
            == 400
        )


def test_kernel_group_bad_op_is_400(monkeypatch) -> None:
    _as_elevated(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("bad op must not reach the kernel")

    monkeypatch.setattr(core_client, "manage_group", must_not_call)
    r = client.post(
        "/admin/kernel/group",
        data={"csrf": _csrf(), "op": "nonsense", "group_id": "x"},
    )
    assert r.status_code == 400


def test_kernel_group_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("non-groot must not manage groups")

    monkeypatch.setattr(core_client, "manage_group", must_not_call)
    r = client.post(
        "/admin/kernel/group",
        data={"csrf": "x", "op": "create", "group_id": "x"},
    )
    assert r.status_code == 403


def test_admin_groups_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("non-groot must not list groups/roles")

    monkeypatch.setattr(core_client, "list_groups", must_not_call)
    monkeypatch.setattr(core_client, "list_roles", must_not_call)
    assert client.get("/admin/groups").status_code == 403
    assert client.get("/admin/roles").status_code == 403


# --- Auth Provider SSO claim mappers (FE-3: ListSsoMap / ManageSsoMap + claims) --


def test_auth_page_shows_claim_config_and_mapping_forms_only_when_elevated(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)

    async def fake_kc():
        return []

    async def fake_sso_map(assertion):
        return core_pb2.ListSsoMapResponse(
            status=core_pb2.CALL_OK,
            mappings=[
                core_pb2.SsoMapView(
                    provider="keycloak", incoming_group="DSI", our_group_id="admins"
                )
            ],
        )

    async def fake_groups(assertion):
        return core_pb2.ListGroupsResponse(
            status=core_pb2.CALL_OK,
            groups=[
                core_pb2.GroupView(id="admins"),
                core_pb2.GroupView(id="staff"),
                core_pb2.GroupView(id="wheel"),
            ],
        )

    from web_channel import kc_admin

    monkeypatch.setattr(kc_admin, "list_users", fake_kc)
    monkeypatch.setattr(core_client, "list_sso_map", fake_sso_map)
    monkeypatch.setattr(core_client, "list_groups", fake_groups)

    # a plain admin READS the map but gets no mutation forms (elevation-only controls)
    _as_groot(monkeypatch)
    r = client.get("/admin/auth")
    assert r.status_code == 200
    assert "SSO claim mappers" in r.text
    assert "DSI" in r.text and "admins" in r.text
    assert 'action="/admin/auth/claims"' not in r.text
    assert 'action="/admin/auth/sso-map"' not in r.text
    assert "elevation required" in r.text.lower()

    _as_elevated(monkeypatch)
    r = client.get("/admin/auth")
    assert 'action="/admin/auth/claims"' in r.text
    assert 'name="groups_claim"' in r.text and 'name="roles_claim"' in r.text
    assert 'action="/admin/auth/sso-map"' in r.text
    assert 'value="staff"' in r.text  # our-group dropdown option
    assert 'value="wheel"' not in r.text  # never an SSO target


def test_auth_page_handles_missing_sso_map_rpc(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_kc():
        return []

    async def boom(assertion):
        raise RuntimeError("kernel unreachable")

    from web_channel import kc_admin

    monkeypatch.setattr(kc_admin, "list_users", fake_kc)
    monkeypatch.setattr(core_client, "list_sso_map", boom)
    monkeypatch.setattr(core_client, "list_groups", boom)
    r = client.get("/admin/auth")
    assert r.status_code == 200
    assert "SSO map unavailable" in r.text


def test_auth_sso_map_put_calls_manage_sso_map(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_elevated(monkeypatch)
    captured: dict = {}

    async def fake_manage(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_sso_map", fake_manage)
    r = client.post(
        "/admin/auth/sso-map",
        data={"csrf": _csrf(), "op": "put", "incoming_group": "DSI", "our_group_id": "admins"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/auth"
    assert captured["op"] == core_pb2.SSO_MAP_PUT
    assert captured["provider"] == "keycloak"
    assert captured["incoming_group"] == "DSI"
    assert captured["our_group_id"] == "admins"


def test_auth_sso_map_delete_calls_manage_sso_map(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_elevated(monkeypatch)
    captured: dict = {}

    async def fake_manage(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_sso_map", fake_manage)
    r = client.post(
        "/admin/auth/sso-map",
        data={"csrf": _csrf(), "op": "delete", "incoming_group": "DSI"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["op"] == core_pb2.SSO_MAP_DELETE
    assert captured["incoming_group"] == "DSI"


def test_auth_claims_persist_and_are_read_back(monkeypatch) -> None:
    _as_elevated(monkeypatch)
    r = client.post(
        "/admin/auth/claims",
        data={"csrf": _csrf(), "groups_claim": "my_groups", "roles_claim": "acme.roles"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert auth.groups_claim() == "my_groups"
    assert auth.roles_claim() == "acme.roles"
    # empty ⇒ back to the built-in default
    client.post(
        "/admin/auth/claims",
        data={"csrf": _csrf(), "groups_claim": "", "roles_claim": ""},
        follow_redirects=False,
    )
    assert auth.groups_claim() == auth.DEFAULT_GROUPS_CLAIM
    assert auth.roles_claim() == auth.DEFAULT_ROLES_CLAIM


def test_auth_sso_map_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("non-groot must not manage the SSO map")

    monkeypatch.setattr(core_client, "manage_sso_map", must_not_call)
    r = client.post("/admin/auth/sso-map", data={"csrf": "x", "op": "put"})
    assert r.status_code == 403
    r = client.post("/admin/auth/claims", data={"csrf": "x", "groups_claim": "g"})
    assert r.status_code == 403


def test_principal_from_claims_honors_configured_groups_claim_and_ignores_roles(
    monkeypatch,
) -> None:
    # a non-default groups claim path is read and normalized; IdP roles NEVER confer authority
    monkeypatch.setenv("OIDC_ROLES_CLAIM", "acme.roles")
    monkeypatch.setenv("OIDC_GROUPS_CLAIM", "acme.groups")
    claims = {
        "sub": "s-1",
        "preferred_username": "bob",
        "acme": {"roles": ["groot"], "groups": ["/DSI"]},
    }
    p = auth.principal_from_claims(claims)
    assert p.is_admin is False and p.is_elevated is False
    assert p.groups == ["DSI"]  # normalized (leading slash stripped)


def test_admin_group_detail_renders_role_and_members_no_scopes(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_get_group(assertion, gid):
        assert gid == "admins"
        return core_pb2.GetGroupResponse(
            status=core_pb2.CALL_OK,
            group=core_pb2.GroupView(
                id="admins", name="Admins", member_count=1, granted_roles=["admin"]
            ),
            members=[
                core_pb2.GroupMember(
                    user_id="u-1", login="alice", providers=["keycloak"], status="active"
                )
            ],
        )

    monkeypatch.setattr(core_client, "get_group", fake_get_group)
    r = client.get("/admin/groups/admins")
    assert r.status_code == 200
    assert "Admins" in r.text and "admin" in r.text
    # no scope surface of any kind — a group grants no visibility
    assert "Connectors (source scopes)" not in r.text
    assert 'value="set_scopes"' not in r.text and "src:" not in r.text
    assert "no</strong> source visibility" in r.text
    # real member list linking to the user page
    assert "alice" in r.text and 'href="/admin/users/u-1"' in r.text


def test_admin_group_detail_unknown_is_404(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_get_group(assertion, gid):
        return core_pb2.GetGroupResponse(status=core_pb2.CALL_NOT_FOUND)

    monkeypatch.setattr(core_client, "get_group", fake_get_group)
    r = client.get("/admin/groups/nope")
    assert r.status_code == 404
    assert "group not found" in r.text


# --- Projects — the sharing object (ADR-20) ---------------------------------------


def test_admin_projects_page_lists_projects_and_offers_public_only_when_elevated(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)

    async def fake_list_projects(assertion, mine_only=False):
        assert assertion.count(".") == 2
        return core_pb2.ListProjectsResponse(
            status=core_pb2.CALL_OK,
            projects=[
                core_pb2.ProjectView(
                    id="p-1",
                    name="Internal",
                    visibility="shared",
                    source_count=2,
                    member_count=1,
                    created_at="2026-08-27T10:00:00Z",
                ),
                core_pb2.ProjectView(
                    id="p-2",
                    name="Handbook",
                    visibility="public",
                    source_count=1,
                    member_count=0,
                    created_at="2026-08-27T10:00:00Z",
                ),
            ],
        )

    monkeypatch.setattr(core_client, "list_projects", fake_list_projects)
    _as_groot(monkeypatch)
    r = client.get("/admin/projects")
    assert r.status_code == 200
    assert 'href="/admin/projects/p-1"' in r.text and "Internal" in r.text and "Handbook" in r.text
    assert 'action="/admin/kernel/project"' in r.text and 'value="create"' in r.text
    assert 'value="public"' not in r.text  # creating AS public needs an elevation

    _as_elevated(monkeypatch)
    r = client.get("/admin/projects")
    assert 'value="public"' in r.text


def test_admin_project_detail_renders_sources_members_and_gates_publicness(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)

    def fake_get(visibility):
        async def _get(assertion, pid):
            assert pid == "p-1"
            return core_pb2.GetProjectResponse(
                status=core_pb2.CALL_OK,
                project=core_pb2.ProjectView(
                    id="p-1",
                    name="Internal",
                    visibility=visibility,
                    source_count=1,
                    member_count=2,
                    created_at="2026-08-27T10:00:00Z",
                ),
                sources=[
                    core_pb2.SourceView(
                        id="s-1",
                        project_id="p-1",
                        kind="wiki",
                        label="Team wiki",
                        scope="src:0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
                        origin="admin",
                        created_at="2026-08-27T10:00:00Z",
                    )
                ],
                members=[
                    core_pb2.ProjectMemberView(user_id="u-1", login="alice", role="owner"),
                    core_pb2.ProjectMemberView(group_id="staff", name="Staff", role="member"),
                ],
            )

        return _get

    monkeypatch.setattr(core_client, "get_project", fake_get("shared"))
    _as_groot(monkeypatch)
    r = client.get("/admin/projects/p-1")
    assert r.status_code == 200
    assert "Team wiki" in r.text and "src:0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000" in r.text
    assert 'href="/admin/users/u-1"' in r.text and 'href="/admin/groups/staff"' in r.text
    assert 'value="add_source"' in r.text and 'value="add_member"' in r.text
    assert 'value="remove_source"' in r.text and 'value="remove_member"' in r.text
    assert 'value="set_visibility"' in r.text
    assert 'value="public"' not in r.text  # publicness needs an elevation

    # a PUBLIC project for a plain admin: no source add/remove, visibility + delete disabled
    monkeypatch.setattr(core_client, "get_project", fake_get("public"))
    r = client.get("/admin/projects/p-1")
    assert 'value="add_source"' not in r.text and 'value="remove_source"' not in r.text
    assert "needs an elevation" in r.text

    _as_elevated(monkeypatch)
    r = client.get("/admin/projects/p-1")
    assert 'value="add_source"' in r.text and 'value="public"' in r.text


def test_admin_project_detail_not_found_is_honest_404(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_get(assertion, pid):
        return core_pb2.GetProjectResponse(status=core_pb2.CALL_NOT_FOUND)

    monkeypatch.setattr(core_client, "get_project", fake_get)
    r = client.get("/admin/projects/nope")
    assert r.status_code == 404
    assert "project not found" in r.text


def test_kernel_project_create_redirects_to_the_new_project(monkeypatch) -> None:
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_manage_project(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK, project_id="p-new")

    monkeypatch.setattr(core_client, "manage_project", fake_manage_project)
    r = client.post(
        "/admin/kernel/project",
        data={
            "csrf": _csrf(),
            "op": "create",
            "name": "Team wiki",
            "description": "pages",
            "visibility": "shared",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/projects/p-new"
    assert captured["op"] == core_pb2.PROJECT_CREATE
    assert captured["name"] == "Team wiki" and captured["visibility"] == "shared"


def test_kernel_project_add_member_resolves_a_login_to_the_kernel_uuid(monkeypatch) -> None:
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_list_users(assertion, include_deleted=False, limit=0, query="", offset=0):
        assert query == "alice"
        return core_pb2.ListUsersResponse(
            status=core_pb2.CALL_OK,
            total=2,
            users=[
                core_pb2.UserView(id="u-alice2", login="alice2"),
                core_pb2.UserView(id="u-alice", login="alice"),
            ],
        )

    async def fake_manage_project(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "list_users", fake_list_users)
    monkeypatch.setattr(core_client, "manage_project", fake_manage_project)
    r = client.post(
        "/admin/kernel/project",
        data={
            "csrf": _csrf(),
            "op": "add_member",
            "project_id": "p-1",
            "member_login": "alice",
            "member_role": "owner",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/projects/p-1"
    assert captured["op"] == core_pb2.PROJECT_ADD_MEMBER
    assert captured["member_user_id"] == "u-alice"  # exact login match, not the prefix twin
    assert captured["member_role"] == "owner"

    # an unknown login never reaches the kernel
    async def fake_none(assertion, include_deleted=False, limit=0, query="", offset=0):
        return core_pb2.ListUsersResponse(status=core_pb2.CALL_OK, total=0, users=[])

    monkeypatch.setattr(core_client, "list_users", fake_none)
    captured.clear()
    r = client.post(
        "/admin/kernel/project",
        data={"csrf": _csrf(), "op": "add_member", "project_id": "p-1", "member_login": "ghost"},
    )
    assert r.status_code == 404 and captured == {}


def test_kernel_project_rejections_render_the_kernel_status(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def denied(assertion, op, **kw):
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_NOT_AUTHORIZED)

    monkeypatch.setattr(core_client, "manage_project", denied)
    r = client.post(
        "/admin/kernel/project",
        data={"csrf": _csrf(), "op": "set_visibility", "project_id": "p-1", "visibility": "public"},
    )
    assert r.status_code == 409 and "not authorized" in r.text
    assert (
        client.post(
            "/admin/kernel/project", data={"csrf": _csrf(), "op": "nonsense", "project_id": "p-1"}
        ).status_code
        == 400
    )


def test_kernel_project_forbidden_for_non_admin(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_admin=False))
    assert client.get("/admin/projects").status_code == 403
    assert (
        client.post("/admin/kernel/project", data={"op": "create", "name": "x"}).status_code == 403
    )


# --- Elevation (sudo) -------------------------------------------------------------


def test_elevate_form_and_end_are_available_to_a_local_account(monkeypatch) -> None:
    _as_groot(monkeypatch)
    r = client.get("/admin/elevate")
    assert r.status_code == 200
    assert 'action="/admin/elevate"' in r.text and 'name="password"' in r.text
    assert 'name="reason"' in r.text
    # the sidebar offers the sudo affordance to a local admin who is not elevated
    page = client.get("/admin")
    assert 'href="/admin/elevate"' in page.text and "End elevation" not in page.text

    _as_elevated(monkeypatch)
    page = client.get("/admin")
    assert "End elevation" in page.text and 'action="/admin/elevation/end"' in page.text


def test_elevate_reverifies_the_password_signs_a_reauth_proof_and_refreshes(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    localusers.create("groot", "correct-horse", [], created_by="t")
    _as_groot(monkeypatch)
    captured: dict = {}

    async def fake_elevate(assertion, reason, reauth, ttl_s=0):
        captured["assertion"] = assertion
        captured["reason"] = reason
        captured["reauth"] = reauth
        return core_pb2.ElevateResponse(
            status=core_pb2.CALL_OK, elevation_id="e-1", expires_at="2026-08-27T23:00:00Z"
        )

    async def fake_resolve(assertion):
        return core_pb2.ResolveActorResponse(
            status=core_pb2.CALL_OK,
            uuid="u-root",
            login="groot",
            scopes=["public"],
            caps=["manage_access", "read_any_conversation", "manage_wheel"],
            elevation_expires_at="2026-08-27T23:00:00Z",
        )

    monkeypatch.setattr(core_client, "elevate", fake_elevate)
    monkeypatch.setattr(core_client, "resolve_actor", fake_resolve)

    # wrong password: no proof is signed, the kernel is never called
    r = client.post(
        "/admin/elevate",
        data={"csrf": _csrf(), "reason": "incident 7", "password": "nope"},
    )
    assert r.status_code == 401 and captured == {}

    # blank reason: refused before any password check
    r = client.post("/admin/elevate", data={"csrf": _csrf(), "reason": "  ", "password": "x"})
    assert r.status_code == 400 and captured == {}

    r = client.post(
        "/admin/elevate",
        data={"csrf": _csrf(), "reason": "incident 7", "password": "correct-horse"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert captured["reason"] == "incident 7"
    # the proof is a distinct, short-lived token with the re-auth audience and a one-time jti
    import base64
    import json

    payload = captured["reauth"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    assert claims["aud"] == "swarm.reauth.v1"
    assert claims["sub"] == "groot" and claims["provider"] == "local" and claims["sid"] == "sess-1"
    assert claims["jti"] and claims["exp"] - claims["iat"] <= 60
    assert abs(claims["auth_time"] - claims["iat"]) <= 2


def test_elevate_refused_by_kernel_is_shown_honestly(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    localusers.create("groot", "pw", [], created_by="t")
    _as_groot(monkeypatch)

    async def refused(assertion, reason, reauth, ttl_s=0):
        return core_pb2.ElevateResponse(status=core_pb2.CALL_NOT_AUTHORIZED)

    monkeypatch.setattr(core_client, "elevate", refused)
    r = client.post("/admin/elevate", data={"csrf": _csrf(), "reason": "r", "password": "pw"})
    assert r.status_code == 403
    assert "Elevation refused" in r.text


def test_elevation_end_calls_the_kernel_and_refreshes(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_elevated(monkeypatch)
    called: dict = {}

    async def fake_end(assertion, elevation_id=""):
        called["assertion"] = assertion
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    async def fake_resolve(assertion):
        return core_pb2.ResolveActorResponse(
            status=core_pb2.CALL_OK,
            uuid="u-root",
            login="groot",
            scopes=["public"],
            caps=["manage_access"],
        )

    monkeypatch.setattr(core_client, "end_elevation", fake_end)
    monkeypatch.setattr(core_client, "resolve_actor", fake_resolve)
    r = client.post("/admin/elevation/end", data={"csrf": _csrf()}, follow_redirects=False)
    assert r.status_code == 303
    assert called["assertion"].count(".") == 2
