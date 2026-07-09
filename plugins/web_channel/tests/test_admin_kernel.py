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
                    id=uid, login="alice", first_name="Alice", last_name="Admin",
                    status="active", roles=["admin"], groups=["everyone"],
                    providers=["local"], last_login_at="2026-07-08T10:00:00Z",
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
    # the roster only links to the detail page — no in-table actions/kebab/UUID
    assert f'href="/admin/users/{uid}"' in r.text
    assert "Manage →" in r.text
    assert 'role="menuitem"' not in r.text
    assert "grant_role" not in r.text and "grant_group" not in r.text
    assert 'value="delete"' not in r.text
    # the truncated-UUID column (phantom duplicates) is gone from the roster
    assert f'title="{uid}"' not in r.text
    assert ">UUID<" not in r.text


def test_admin_user_detail_renders_identity_and_actions(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
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
                groups=["confluence"],
                providers=["local"],
                last_login_at="2026-07-08T10:00:00Z",
                emails=["alice@example.org"],
            ),
        )

    monkeypatch.setattr(core_client, "get_user", fake_get_user)
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
    assert 'value="grant_group"' in r.text  # alice is in none of the canonical 3 → Add
    assert 'value="everyone"' in r.text and 'value="admins"' in r.text
    assert 'value="superuser"' in r.text  # shown because alice is a local user
    # lifecycle on the detail page
    assert 'action="/admin/kernel/user"' in r.text
    assert 'value="deactivate"' in r.text and 'value="delete"' in r.text
    # local-credential section moved here from the Users list
    assert "Local channel credential" in r.text
    assert "Knowledge" in r.text and "(planned)" in r.text


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
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("detail page must not fetch a user for a non-groot")

    monkeypatch.setattr(core_client, "get_user", must_not_call)
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


# --- Groups & Roles (FE-2: ListGroups / ListRoles / ManageGroup) -------------


def test_admin_groups_page_is_read_only_list_plus_baseline(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setenv("SWARM_AUTH_BASELINE_GROUP", "everyone")
    monkeypatch.setenv("KNOWN_SOURCE_SCOPES", "src:wiki,src:ldap,private")
    _as_groot(monkeypatch)

    async def fake_list_groups(assertion):
        assert assertion.count(".") == 2
        return core_pb2.ListGroupsResponse(
            status=core_pb2.CALL_OK,
            groups=[
                core_pb2.GroupView(
                    id="everyone",
                    name="Everyone",
                    member_count=3,
                    granted_scopes=["public", "src:wiki"],
                    granted_roles=[],
                ),
                core_pb2.GroupView(
                    id="admins", name="Admins", member_count=1, granted_roles=["admin"]
                ),
            ],
        )

    monkeypatch.setattr(core_client, "list_groups", fake_list_groups)
    r = client.get("/admin/groups")
    assert r.status_code == 200
    assert "Everyone" in r.text and "Admins" in r.text
    assert "baseline" in r.text
    # list is read-only + links to each group's page — no create/rename/delete/set-role in the list
    assert 'href="/admin/groups/everyone"' in r.text
    assert 'href="/admin/groups/admins"' in r.text
    assert "value=\"rename\"" not in r.text and "value=\"delete\"" not in r.text
    assert "value=\"set_role\"" not in r.text and "New group" not in r.text
    # only the baseline (Everyone) scope control remains on this page (set_scopes for everyone)
    assert 'value="set_scopes"' in r.text
    assert 'value="src:wiki"' in r.text and 'value="src:ldap"' in r.text
    assert 'value="private"' not in r.text  # never grantable
    assert 'value="src:wiki" checked' in r.text  # baseline holds it → pre-checked


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


def test_kernel_group_set_scopes_calls_manage_group_dropping_private(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)
    captured = {}

    async def fake_manage_group(assertion, op, **kw):
        captured["op"] = op
        captured.update(kw)
        return core_pb2.AdminActionResponse(status=core_pb2.CALL_OK)

    monkeypatch.setattr(core_client, "manage_group", fake_manage_group)
    r = client.post(
        "/admin/kernel/group",
        data={
            "csrf": _csrf(),
            "op": "set_scopes",
            "group_id": "everyone",
            "scopes": ["public", "src:wiki", "private"],  # repeated form fields
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/groups/everyone"  # back to the group detail
    assert captured["op"] == core_pb2.GROUP_SET_SCOPES
    assert captured["group_id"] == "everyone"
    assert captured["scopes"] == ["public", "src:wiki"]  # private dropped defensively


def test_kernel_group_bad_op_is_400(monkeypatch) -> None:
    _as_groot(monkeypatch)

    async def must_not_call(*a, **kw):
        raise AssertionError("bad op must not reach the kernel")

    monkeypatch.setattr(core_client, "manage_group", must_not_call)
    r = client.post(
        "/admin/kernel/group",
        data={"csrf": _csrf(), "op": "nonsense", "group_id": "x"},
    )
    assert r.status_code == 400


def test_kernel_group_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("non-groot must not manage groups")

    monkeypatch.setattr(core_client, "manage_group", must_not_call)
    r = client.post(
        "/admin/kernel/group",
        data={"csrf": "x", "op": "create", "group_id": "x"},
    )
    assert r.status_code == 403


def test_admin_groups_forbidden_for_non_groot(monkeypatch) -> None:
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("non-groot must not list groups/roles")

    monkeypatch.setattr(core_client, "list_groups", must_not_call)
    monkeypatch.setattr(core_client, "list_roles", must_not_call)
    assert client.get("/admin/groups").status_code == 403
    assert client.get("/admin/roles").status_code == 403


# --- Auth Provider SSO claim mappers (FE-3: ListSsoMap / ManageSsoMap + claims) --


def test_auth_page_shows_claim_config_and_mapping(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

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
            groups=[core_pb2.GroupView(id="admins"), core_pb2.GroupView(id="everyone")],
        )

    from web_channel import kc_admin

    monkeypatch.setattr(kc_admin, "list_users", fake_kc)
    monkeypatch.setattr(core_client, "list_sso_map", fake_sso_map)
    monkeypatch.setattr(core_client, "list_groups", fake_groups)
    r = client.get("/admin/auth")
    assert r.status_code == 200
    assert "SSO claim mappers" in r.text
    assert 'action="/admin/auth/claims"' in r.text
    assert 'name="groups_claim"' in r.text and 'name="roles_claim"' in r.text
    assert 'action="/admin/auth/sso-map"' in r.text
    assert "DSI" in r.text and "admins" in r.text
    assert 'value="everyone"' in r.text  # our-group dropdown option


def test_auth_page_handles_missing_sso_map_rpc(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_kc():
        return []

    async def boom(assertion):
        raise RuntimeError("UNIMPLEMENTED: kernel predates BE-1")

    from web_channel import kc_admin

    monkeypatch.setattr(kc_admin, "list_users", fake_kc)
    monkeypatch.setattr(core_client, "list_sso_map", boom)
    monkeypatch.setattr(core_client, "list_groups", boom)
    r = client.get("/admin/auth")
    assert r.status_code == 200
    assert "SSO-map RPC not available yet" in r.text


def test_auth_sso_map_put_calls_manage_sso_map(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)
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
    _as_groot(monkeypatch)
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
    _as_groot(monkeypatch)
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
    monkeypatch.setattr(web, "_current_principal", lambda request: _principal(is_groot=False))

    async def must_not_call(*a, **kw):
        raise AssertionError("non-groot must not manage the SSO map")

    monkeypatch.setattr(core_client, "manage_sso_map", must_not_call)
    r = client.post("/admin/auth/sso-map", data={"csrf": "x", "op": "put"})
    assert r.status_code == 403
    r = client.post("/admin/auth/claims", data={"csrf": "x", "groups_claim": "g"})
    assert r.status_code == 403


def test_principal_from_claims_honors_configured_roles_claim(monkeypatch) -> None:
    # a non-default roles claim path resolves groot correctly
    monkeypatch.setenv("OIDC_ROLES_CLAIM", "acme.roles")
    monkeypatch.setenv("OIDC_GROUPS_CLAIM", "acme.groups")
    claims = {
        "sub": "s-1",
        "preferred_username": "bob",
        "acme": {"roles": ["groot"], "groups": ["/DSI"]},
    }
    p = auth.principal_from_claims(claims)
    assert p.is_groot is True
    assert p.groups == ["DSI"]  # normalized (leading slash stripped)


# --- Baseline (Everyone) scopes control (FE-4) -------------------------------


def test_admin_groups_shows_baseline_everyone_control(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setenv("SWARM_AUTH_BASELINE_GROUP", "everyone")
    monkeypatch.setenv("KNOWN_SOURCE_SCOPES", "src:wiki,src:ldap,src:confluence")
    _as_groot(monkeypatch)

    async def fake_list_groups(assertion):
        return core_pb2.ListGroupsResponse(
            status=core_pb2.CALL_OK,
            groups=[
                core_pb2.GroupView(
                    id="everyone", name="Everyone", member_count=9,
                    granted_scopes=["public", "src:wiki", "src:ldap"],
                ),
            ],
        )

    monkeypatch.setattr(core_client, "list_groups", fake_list_groups)
    r = client.get("/admin/groups")
    assert r.status_code == 200
    assert "Baseline access" in r.text
    assert "every authenticated user" in r.text
    # the set-baseline-scopes form targets the everyone group with its scopes pre-checked
    assert 'value="everyone"' in r.text
    assert 'value="src:wiki" checked' in r.text
    assert 'value="src:ldap" checked' in r.text
    assert 'value="src:confluence"' in r.text and 'value="src:confluence" checked' not in r.text
    assert "Set baseline scopes" in r.text


def test_admin_group_detail_renders_scopes_and_pending_members(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setenv("KNOWN_SOURCE_SCOPES", "src:wiki,src:ldap")
    _as_groot(monkeypatch)

    async def fake_list_groups(assertion):
        return core_pb2.ListGroupsResponse(
            status=core_pb2.CALL_OK,
            groups=[
                core_pb2.GroupView(
                    id="admins", name="Admins", member_count=2,
                    granted_roles=["admin"], granted_scopes=["public", "src:wiki"],
                ),
            ],
        )

    monkeypatch.setattr(core_client, "list_groups", fake_list_groups)
    r = client.get("/admin/groups/admins")
    assert r.status_code == 200
    assert "Admins" in r.text
    assert "Connectors (source scopes)" in r.text
    assert 'value="set_scopes"' in r.text
    assert 'value="src:wiki" checked' in r.text and 'value="src:ldap"' in r.text
    assert "pending kernel RPC" in r.text  # member list honest-deferred to Track B


def test_admin_group_detail_unknown_is_404(monkeypatch) -> None:
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    _as_groot(monkeypatch)

    async def fake_list_groups(assertion):
        return core_pb2.ListGroupsResponse(status=core_pb2.CALL_OK, groups=[])

    monkeypatch.setattr(core_client, "list_groups", fake_list_groups)
    r = client.get("/admin/groups/nope")
    assert r.status_code == 404
    assert "group not found" in r.text
