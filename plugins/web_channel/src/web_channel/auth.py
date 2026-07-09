"""OIDC identity for web_channel (P1). Keycloak (OIDC) is primary.

The channel owns identity→scope mapping but NOT scope enforcement: it maps the
authenticated user's IdP groups to kernel scopes (default-deny) and passes an
authenticated viewer+scopes to the kernel. The kernel remains the sole scope
authority. Swap to a real deployment by pointing OIDC_ISSUER at the org's real
Keycloak realm (config only — never a hardcoded realm URL here).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from authlib.integrations.starlette_client import OAuth

from web_channel import settings

PUBLIC_SCOPE = "public"
GROOT_ROLE = "groot"


def oidc_enabled() -> bool:
    return os.environ.get("OIDC_ENABLED", "false").lower() == "true"


def _group_scope_map() -> dict[str, str]:
    """group→scope map from GROUP_SCOPE_MAP (JSON). Malformed/empty ⇒ {} (no widening)."""
    raw = os.environ.get("GROUP_SCOPE_MAP", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


def known_groups() -> list[str]:
    """Groups the channel knows how to map to a scope (for the groot invite form)."""
    return list(_group_scope_map().keys())


# The decided fixed group set (ADR authz model, 2026-07-09): exactly these three,
# roles attach to the GROUP, `superadmin` only on Superuser, and Superuser takes
# local-provider members only. ids are key-safe/lowercase; names are for display.
CANONICAL_GROUPS = [
    {"id": "superuser", "name": "Superuser", "role": "superadmin", "local_only": True},
    {"id": "admins", "name": "Admins", "role": "admin", "local_only": False},
    {"id": "everyone", "name": "Everyone", "role": "user", "local_only": False},
]


def canonical_groups() -> list[dict]:
    """The fixed Superuser/Admins/Everyone set (copies, safe to mutate)."""
    return [dict(g) for g in CANONICAL_GROUPS]


def baseline_group() -> str:
    """The kernel-authz baseline group whose scopes every authenticated actor
    inherits (the "Everyone" baseline; SWARM_AUTH_BASELINE_GROUP). "" ⇒ none."""
    return os.environ.get("SWARM_AUTH_BASELINE_GROUP", "").strip()


def known_source_scopes() -> list[str]:
    """Candidate scopes for the admin Groups scope-picker (ADR-18): always
    `public`, plus each entry in KNOWN_SOURCE_SCOPES (comma-separated, e.g.
    `src:wiki,src:ldap`). `private` is never offered — a group cannot confer it
    (the kernel hard-denies it at the grant boundary regardless)."""
    raw = os.environ.get("KNOWN_SOURCE_SCOPES", "").strip()
    out = [PUBLIC_SCOPE]
    for candidate in (s.strip() for s in raw.split(",")):
        if candidate and candidate != "private" and candidate not in out:
            out.append(candidate)
    return out


def scopes_for(groups: list[str]) -> list[str]:
    """Map IdP groups → kernel scopes. ALWAYS includes `public`; adds a mapped scope
    per KNOWN group; an unknown group grants nothing (default-deny). Deduped, stable
    order (public first). This is the load-bearing no-leak boundary on the channel side.
    """
    mapping = _group_scope_map()
    scopes = [PUBLIC_SCOPE]
    for g in groups or []:
        mapped = mapping.get(g)
        if mapped and mapped not in scopes:
            scopes.append(mapped)
    return scopes


@dataclass
class Principal:
    viewer: str
    scopes: list[str]
    groups: list[str] = field(default_factory=list)
    is_groot: bool = False
    display: str = ""
    # ADR-16 D9 — the identity the channel SIGNS an actor assertion for
    # (`sub`+`provider`), and the kernel-DERIVED result of that assertion once
    # `ResolveActor` has verified it (`uuid`/`caps`; never trusted from elsewhere).
    # `sid` is a per-login opaque session id (not the cookie itself), minted at
    # login and carried in every assertion so a replay is at least bound to it.
    sub: str = ""
    provider: str = ""
    sid: str = ""
    uuid: str = ""
    caps: list[str] = field(default_factory=list)

    def to_session(self) -> dict:
        return asdict(self)

    @classmethod
    def from_session(cls, data: dict) -> Principal:
        return cls(
            viewer=data.get("viewer", ""),
            scopes=list(data.get("scopes", [PUBLIC_SCOPE])),
            groups=list(data.get("groups", [])),
            is_groot=bool(data.get("is_groot", False)),
            display=data.get("display", ""),
            sub=data.get("sub", ""),
            provider=data.get("provider", ""),
            sid=data.get("sid", ""),
            uuid=data.get("uuid", ""),
            caps=list(data.get("caps", [])),
        )


DEFAULT_GROUPS_CLAIM = "groups"
DEFAULT_ROLES_CLAIM = "realm_access.roles"


def sso_provider() -> str:
    """The provider key SSO-group mappings are stored under (must match the provider
    the kernel provisions SSO subjects with; ADR-16 D3). Keycloak by default."""
    return settings.get_or_env("OIDC_PROVIDER", "keycloak")


def groups_claim() -> str:
    """Which id-token claim carries the user's groups (dotted path allowed). The
    operator sets this on /admin/auth; default `groups`."""
    return settings.get_or_env("OIDC_GROUPS_CLAIM", DEFAULT_GROUPS_CLAIM) or DEFAULT_GROUPS_CLAIM


def roles_claim() -> str:
    """Which id-token claim carries the user's roles (dotted path allowed). Default
    `realm_access.roles` (Keycloak's realm roles)."""
    return settings.get_or_env("OIDC_ROLES_CLAIM", DEFAULT_ROLES_CLAIM) or DEFAULT_ROLES_CLAIM


def _dig(claims: dict, dotted: str) -> object:
    """Walk a dotted claim path (`realm_access.roles`); None if any segment misses."""
    cur: object = claims
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _claim_list(claims: dict, dotted: str) -> list:
    value = _dig(claims, dotted)
    return list(value) if isinstance(value, list) else []


def principal_from_claims(claims: dict) -> Principal:
    """Build a Principal from verified OIDC id-token claims. Scopes are DERIVED from
    groups here (never taken from the client/token directly), so the channel decides
    scope from identity, deterministically. `sub` is the IdP's stable subject (never
    the display name) — the identity the actor assertion is signed for (ADR-16 D9).
    WHICH claim carries groups/roles is operator-configured (/admin/auth claim keys)."""
    # Normalize: strip whitespace and a leading "/" (Keycloak emits "/confluence"
    # when the groups mapper uses full paths) so map lookups are robust.
    groups = [str(g).strip().lstrip("/") for g in _claim_list(claims, groups_claim())]
    roles = _claim_list(claims, roles_claim())
    viewer = claims.get("preferred_username") or claims.get("sub") or ""
    display = claims.get("name") or viewer
    return Principal(
        viewer=viewer,
        scopes=scopes_for(groups),
        groups=groups,
        is_groot=GROOT_ROLE in roles,
        display=display,
        sub=claims.get("sub") or viewer,
        provider="keycloak",
    )


_oauth: OAuth | None = None


def oauth() -> OAuth:
    """Lazily-built authlib OAuth registry for the Keycloak OIDC provider."""
    global _oauth
    if _oauth is None:
        registry = OAuth()
        issuer = settings.get_or_env("OIDC_ISSUER").rstrip("/")
        registry.register(
            name="kc",
            server_metadata_url=f"{issuer}/.well-known/openid-configuration",
            client_id=settings.get_or_env("OIDC_CLIENT_ID"),
            client_secret=settings.get_or_env("OIDC_CLIENT_SECRET"),
            client_kwargs={"scope": "openid profile email"},
        )
        _oauth = registry
    return _oauth


def _reset_oauth() -> None:
    global _oauth
    _oauth = None


settings.register_on_change(_reset_oauth)
