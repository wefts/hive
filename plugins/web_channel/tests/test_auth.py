"""Unit tests for the identity→scope mapping (the channel-side no-leak boundary)."""

from __future__ import annotations

from web_channel import auth


def test_scopes_for_default_deny(monkeypatch) -> None:
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    # known group → mapped scope (plus public)
    assert auth.scopes_for(["confluence"]) == ["public", "group"]
    # no group → public only
    assert auth.scopes_for([]) == ["public"]
    # UNKNOWN group grants nothing beyond public (default-deny) — the load-bearing rule
    assert auth.scopes_for(["secret-cabal"]) == ["public"]
    # dedup + stable order
    assert auth.scopes_for(["confluence", "confluence"]) == ["public", "group"]


def test_scopes_for_malformed_map_is_safe(monkeypatch) -> None:
    # A malformed map must NOT widen scope — it collapses to public-only.
    monkeypatch.setenv("GROUP_SCOPE_MAP", "not json{")
    assert auth.scopes_for(["confluence"]) == ["public"]
    monkeypatch.setenv("GROUP_SCOPE_MAP", "[1,2,3]")  # not an object
    assert auth.scopes_for(["confluence"]) == ["public"]
    monkeypatch.delenv("GROUP_SCOPE_MAP", raising=False)
    assert auth.scopes_for(["confluence"]) == ["public"]


def test_principal_from_claims_alice_bob_groot(monkeypatch) -> None:
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    alice = auth.principal_from_claims(
        {"preferred_username": "alice", "groups": ["confluence"], "realm_access": {"roles": []}}
    )
    assert alice.viewer == "alice"
    assert alice.scopes == ["public", "group"]
    assert alice.is_groot is False

    bob = auth.principal_from_claims({"preferred_username": "bob", "groups": []})
    assert bob.scopes == ["public"]  # no group → public only
    assert bob.is_groot is False

    groot = auth.principal_from_claims(
        {
            "preferred_username": "groot",
            "groups": ["confluence"],
            "realm_access": {"roles": ["groot"]},
        }
    )
    assert groot.is_groot is True


def test_principal_viewer_falls_back_to_sub() -> None:
    p = auth.principal_from_claims({"sub": "abc-123", "groups": []})
    assert p.viewer == "abc-123"


def test_principal_normalizes_full_path_groups(monkeypatch) -> None:
    # If Keycloak emits full group paths ("/confluence") or stray whitespace, the
    # mapping must still match (robustness; never a silent deny on a real grant).
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group"}')
    p = auth.principal_from_claims(
        {"preferred_username": "a", "groups": ["/confluence", " confluence "]}
    )
    assert p.scopes == ["public", "group"]


def test_principal_session_round_trip() -> None:
    p = auth.principal_from_claims(
        {"preferred_username": "x", "groups": [], "realm_access": {"roles": ["groot"]}}
    )
    back = auth.Principal.from_session(p.to_session())
    assert back == p


def test_known_groups(monkeypatch) -> None:
    monkeypatch.setenv("GROUP_SCOPE_MAP", '{"confluence":"group","ops":"group"}')
    assert sorted(auth.known_groups()) == ["confluence", "ops"]
