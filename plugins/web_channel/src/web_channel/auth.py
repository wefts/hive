"""OIDC identity for web_channel (P1) — authentication only.

The channel authenticates (Keycloak OIDC or the local credential store) and SIGNS an actor
assertion; the KERNEL derives scopes and capabilities from its own records (workspace
ADR-16 D9, ADR-20). Nothing here maps groups to scopes any more: `Principal.scopes` is
whatever `ResolveActor` returned, `is_admin` / `is_elevated` reflect kernel-derived caps.
Swap to a real deployment by pointing OIDC_ISSUER at the org's real Keycloak realm
(config only — never a hardcoded realm URL here).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from authlib.integrations.starlette_client import OAuth

from web_channel import settings

PUBLIC_SCOPE = "public"

# The capabilities the kernel confers (ADR-20 §5). `ADMIN_CAPS` = any of these opens the
# admin console; `ELEVATED_CAPS` exist only under a live, session-bound elevation.
ADMIN_CAPS = ("manage_access", "invite_users", "manage_users", "manage_projects")
ELEVATED_CAPS = (
    "read_any_conversation",
    "manage_wheel",
    "manage_roles",
    "manage_auth",
    "manage_publicness",
)


def oidc_enabled() -> bool:
    return os.environ.get("OIDC_ENABLED", "false").lower() == "true"


# The FIXED group set (workspace ADR-20 D7): exactly these three. Groups carry ROLES
# only, never source visibility (that is Project membership). `wheel` is local-only and
# managed under an elevation; `admins` confers `admin`; `staff` is the default internal
# cohort (no role). ids are key-safe/lowercase; names are for display.
CANONICAL_GROUPS = [
    {
        "id": "wheel",
        "name": "Wheel",
        "role": "elevate",
        "local_only": True,
        "elevation_only": True,
    },
    {
        "id": "admins",
        "name": "Admins",
        "role": "admin",
        "local_only": False,
        "elevation_only": False,
    },
    {"id": "staff", "name": "Staff", "role": "—", "local_only": False, "elevation_only": False},
]


def canonical_groups() -> list[dict]:
    """The fixed wheel/admins/staff set (copies, safe to mutate)."""
    return [dict(g) for g in CANONICAL_GROUPS]


def is_admin_caps(caps: list[str]) -> bool:
    """Any admin capability opens the console (the kernel is the enforcer of each op)."""
    return any(c in caps for c in ADMIN_CAPS)


def is_elevated_caps(caps: list[str]) -> bool:
    """A live elevation is visible as the superadmin-only capabilities."""
    return "read_any_conversation" in caps


@dataclass
class Principal:
    viewer: str
    scopes: list[str]
    groups: list[str] = field(default_factory=list)
    # ADR-20: the admin-console gate (ANY admin cap) and the elevation state, BOTH
    # reflections of kernel-derived caps — never an IdP role, never a local flag.
    is_admin: bool = False
    is_elevated: bool = False
    elevation_expires_at: str = ""
    external: bool = False
    display: str = ""
    # ADR-16 D9 — the identity the channel SIGNS an actor assertion for
    # (`sub`+`provider`), and the kernel-DERIVED result of that assertion once
    # `ResolveActor` has verified it (`uuid`/`caps`; never trusted from elsewhere).
    # `sid` is a per-login opaque session id (not the cookie itself), minted at
    # login and carried in every assertion — an elevation is bound to it.
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
            is_admin=bool(data.get("is_admin", False)),
            is_elevated=bool(data.get("is_elevated", False)),
            elevation_expires_at=str(data.get("elevation_expires_at", "") or ""),
            external=bool(data.get("external", False)),
            display=data.get("display", ""),
            sub=data.get("sub", ""),
            provider=data.get("provider", ""),
            sid=data.get("sid", ""),
            uuid=data.get("uuid", ""),
            caps=list(data.get("caps", [])),
        )

    def apply_resolved(
        self,
        uuid: str,
        scopes: list[str],
        caps: list[str],
        elevation_expires_at: str = "",
        external: bool = False,
    ) -> None:
        """Adopt the kernel's DERIVED view of this actor (the only authority)."""
        self.uuid = uuid
        self.scopes = list(scopes) or [PUBLIC_SCOPE]
        self.caps = list(caps)
        self.is_admin = is_admin_caps(self.caps)
        self.is_elevated = is_elevated_caps(self.caps)
        self.elevation_expires_at = elevation_expires_at or ""
        self.external = bool(external)


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
    `realm_access.roles` (Keycloak's realm roles). DIAGNOSTIC only — IdP roles never
    confer authority (ADR-19 D6)."""
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
    """Build a Principal from verified OIDC id-token claims. The channel decides NOTHING
    about scope or capability here: the groups are forwarded INSIDE the signed provision
    token (the kernel maps them through its own SSO-group map) and the scopes/caps come
    back from `ResolveActor`. Until then the principal is public-only, not an admin.
    `sub` is the IdP's stable subject (never the display name) — the identity the actor
    assertion is signed for (ADR-16 D9)."""
    # Normalize: strip whitespace and a leading "/" (Keycloak emits "/confluence"
    # when the groups mapper uses full paths) so kernel-side map lookups are robust.
    groups = [str(g).strip().lstrip("/") for g in _claim_list(claims, groups_claim())]
    viewer = claims.get("preferred_username") or claims.get("sub") or ""
    display = claims.get("name") or viewer
    return Principal(
        viewer=viewer,
        scopes=[PUBLIC_SCOPE],
        groups=groups,
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
