"""Unit tests for the channel-side identity model (ADR-20): the channel authenticates and
signs; the KERNEL derives scopes and capabilities. Nothing here may widen anything."""

from __future__ import annotations

from web_channel import auth


def test_principal_from_claims_is_public_only_and_not_admin() -> None:
    # Groups are FORWARDED (signed, inside the provision token) — never mapped to scopes here.
    alice = auth.principal_from_claims(
        {"preferred_username": "alice", "groups": ["confluence"], "realm_access": {"roles": []}}
    )
    assert alice.viewer == "alice"
    assert alice.scopes == ["public"]
    assert alice.groups == ["confluence"]
    assert alice.is_admin is False
    assert alice.is_elevated is False
    assert alice.provider == "keycloak"

    # an IdP realm role is DIAGNOSTIC only — it confers no authority (ADR-19 D6 / ADR-20)
    groot = auth.principal_from_claims(
        {
            "preferred_username": "groot",
            "groups": ["confluence"],
            "realm_access": {"roles": ["groot", "admin"]},
        }
    )
    assert groot.is_admin is False
    assert groot.is_elevated is False
    assert groot.scopes == ["public"]


def test_principal_viewer_falls_back_to_sub() -> None:
    p = auth.principal_from_claims({"sub": "abc-123", "groups": []})
    assert p.viewer == "abc-123"
    assert p.sub == "abc-123"


def test_principal_normalizes_full_path_groups() -> None:
    # If Keycloak emits full group paths ("/confluence") or stray whitespace, the forwarded
    # group names are normalized so the KERNEL's SSO map can match them.
    p = auth.principal_from_claims(
        {"preferred_username": "a", "groups": ["/confluence", " confluence "]}
    )
    assert p.groups == ["confluence", "confluence"]


def test_principal_session_round_trip() -> None:
    p = auth.principal_from_claims(
        {"preferred_username": "x", "groups": [], "realm_access": {"roles": ["groot"]}}
    )
    p.apply_resolved(
        "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
        ["public", "src:0192aaaa-bbbb-7ccc-8ddd-eeeeffff0001"],
        ["manage_access", "read_any_conversation"],
        "2026-08-27T21:00:00Z",
        False,
    )
    back = auth.Principal.from_session(p.to_session())
    assert back == p
    assert back.is_admin and back.is_elevated
    assert back.elevation_expires_at == "2026-08-27T21:00:00Z"


def test_apply_resolved_derives_admin_and_elevation_from_kernel_caps() -> None:
    p = auth.principal_from_claims({"preferred_username": "x", "groups": []})

    # a plain user: no caps ⇒ not admin, not elevated
    p.apply_resolved("u-1", ["public"], [], "", False)
    assert p.is_admin is False and p.is_elevated is False

    # any admin cap opens the console; no elevation cap ⇒ not elevated
    p.apply_resolved("u-1", ["public"], ["invite_users"], "", False)
    assert p.is_admin is True and p.is_elevated is False

    # the superadmin-only caps mark a LIVE elevation (session-bound kernel-side)
    p.apply_resolved("u-1", ["public"], ["manage_access", "read_any_conversation"], "later", False)
    assert p.is_admin is True and p.is_elevated is True
    assert p.elevation_expires_at == "later"

    # a guest
    p.apply_resolved("u-2", ["public"], [], "", True)
    assert p.external is True

    # an empty scope list can never widen: it collapses to public
    p.apply_resolved("u-1", [], [], "", False)
    assert p.scopes == ["public"]


def test_canonical_groups_are_the_fixed_three() -> None:
    ids = [g["id"] for g in auth.canonical_groups()]
    assert ids == ["wheel", "admins", "staff"]
    wheel = next(g for g in auth.canonical_groups() if g["id"] == "wheel")
    assert wheel["local_only"] and wheel["elevation_only"]
    # copies, safe to mutate
    auth.canonical_groups()[0]["id"] = "x"
    assert auth.canonical_groups()[0]["id"] == "wheel"


def test_no_channel_side_scope_mapping_exists() -> None:
    # The old GROUP_SCOPE_MAP / baseline / scope-picker helpers are gone: the kernel is the
    # sole authority (a channel bug can no longer widen a scope).
    for name in (
        "scopes_for",
        "known_groups",
        "known_source_scopes",
        "baseline_group",
        "GROOT_ROLE",
    ):
        assert not hasattr(auth, name), name
