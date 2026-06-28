"""OIDC identity for web_channel (P1). Keycloak (OIDC) is primary.

The channel owns identity→scope mapping but NOT scope enforcement: it maps the
authenticated user's IdP groups to kernel scopes (default-deny) and passes an
authenticated viewer+scopes to the kernel. The kernel remains the sole scope
authority. Swap to prod = point OIDC_ISSUER at https://sso.smile.eu/realms/Smile.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field

from authlib.integrations.starlette_client import OAuth

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
        )


def principal_from_claims(claims: dict) -> Principal:
    """Build a Principal from verified OIDC id-token claims. Scopes are DERIVED from
    groups here (never taken from the client/token directly), so the channel decides
    scope from identity, deterministically."""
    # Normalize: strip whitespace and a leading "/" (Keycloak emits "/confluence"
    # when the groups mapper uses full paths) so map lookups are robust.
    groups = [str(g).strip().lstrip("/") for g in (claims.get("groups") or [])]
    realm_access = claims.get("realm_access") or {}
    roles = realm_access.get("roles") or []
    viewer = claims.get("preferred_username") or claims.get("sub") or ""
    display = claims.get("name") or viewer
    return Principal(
        viewer=viewer,
        scopes=scopes_for(groups),
        groups=groups,
        is_groot=GROOT_ROLE in roles,
        display=display,
    )


_oauth: OAuth | None = None


def oauth() -> OAuth:
    """Lazily-built authlib OAuth registry for the Keycloak OIDC provider."""
    global _oauth
    if _oauth is None:
        registry = OAuth()
        issuer = os.environ["OIDC_ISSUER"].rstrip("/")
        registry.register(
            name="kc",
            server_metadata_url=f"{issuer}/.well-known/openid-configuration",
            client_id=os.environ["OIDC_CLIENT_ID"],
            client_secret=os.environ["OIDC_CLIENT_SECRET"],
            client_kwargs={"scope": "openid profile email"},
        )
        _oauth = registry
    return _oauth
