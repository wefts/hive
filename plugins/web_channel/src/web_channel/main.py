"""web_channel — FastAPI operator console over the kernel Core API.

GET /            the input box (index); requires sign-in when OIDC is enabled
POST /ask        runs Core.Ask, returns the answer-card HTMX partial
GET /login       start OIDC login (P1)
GET /auth/callback   OIDC redirect back; stores the session principal
GET /logout      clear the session
GET /healthz     liveness (no kernel call)

Rendering is deterministic (see render.py); Jinja2 autoescape keeps every value
verbatim and HTML-safe. The channel holds no cognition and never reads the graph DB.

Identity: when OIDC is enabled (P1), the authenticated user's viewer + scopes
(derived from IdP groups, see auth.py) drive Ask; the kernel stays the sole scope
authority. When OIDC is off (P0), a fixed operator + public scope is used.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from grpc import aio
from starlette.middleware.sessions import SessionMiddleware

from web_channel import auth, core_client, kc_admin, render
from web_channel._gen import core_pb2

logger = logging.getLogger("web_channel")
_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

# Known placeholders that must NEVER be used as a real signing key.
_PLACEHOLDER_SECRETS = {
    "",
    "dev-insecure-session-secret",
    "dev-session-secret-CHANGE-IN-PROD",
}


def _session_secret() -> str:
    """The session-cookie signing key. The cookie carries the principal (incl.
    is_groot + scopes), so a known/committed key would let anyone FORGE authorization
    (council: codex + 2 lenses). We therefore NEVER ship a default: a real value is
    used as-is; otherwise we mint an ephemeral random key (sessions reset on restart)."""
    configured = os.environ.get("SESSION_SECRET", "").strip()
    if configured and configured not in _PLACEHOLDER_SECRETS:
        return configured
    logger.warning(
        "SESSION_SECRET unset or placeholder — using an ephemeral random key; "
        "sessions reset on restart. Set SESSION_SECRET (secrets.env) for persistence."
    )
    return secrets.token_urlsafe(48)


def _validate_oidc_config() -> None:
    """Fail fast at startup (not mid-login) if OIDC is on but misconfigured."""
    if not auth.oidc_enabled():
        return
    missing = [
        k for k in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET") if not os.environ.get(k)
    ]
    if missing:
        raise RuntimeError(f"OIDC_ENABLED=true but missing required env: {', '.join(missing)}")


_validate_oidc_config()

app = FastAPI(title="Swarm web_channel", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    same_site="lax",
    # Bound staleness: cached groups/roles can't outlive this, so a Keycloak
    # revocation takes effect within the window (council: codex). Prod hardening:
    # shorten further or re-derive authorization per request.
    max_age=int(os.environ.get("SESSION_MAX_AGE_S", "3600")),
    # Must be True behind TLS in prod (set WEB_CHANNEL_HTTPS=true); http for local dev.
    https_only=os.environ.get("WEB_CHANNEL_HTTPS", "false").lower() == "true",
)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


def _viewer() -> str:
    # Fixed operator identity used ONLY when OIDC is off (P0 mode).
    return os.environ.get("SWARM_VIEWER", "operator")


def _scopes() -> list[str]:
    # P0 (OIDC off) is PRE-AUTH: hard-locked to public scope — no env may widen it.
    # When OIDC is on, scopes come from the authenticated principal (auth.scopes_for).
    return ["public"]


def _current_principal(request: Request) -> auth.Principal | None:
    data = request.session.get("user")
    return auth.Principal.from_session(data) if data else None


def _base_url(request: Request) -> str:
    # Configured public base so the OIDC redirect_uri matches a registered URI.
    configured = os.environ.get("WEB_CHANNEL_BASE_URL", "").strip()
    return configured.rstrip("/") if configured else str(request.base_url).rstrip("/")


def _answer_view(resp: core_pb2.AskResponse) -> dict:
    """Structured AskResponse → template context. Values pass verbatim; the template
    autoescapes them. Confidence/citations are shown only where honest."""
    label, status_class = render.status_label(resp.status)
    show_conf = render.show_confidence(resp.status)
    return {
        "answer": resp.answer,
        "status_label": label,
        "status_class": status_class,
        "tier": resp.tier,
        "show_confidence": show_conf,
        "confidence": resp.confidence,
        "confidence_class": render.confidence_class(resp.confidence),
        # Never fabricate evidence on a non-found result.
        "citations": list(resp.citations) if show_conf else [],
    }


def _error_view(detail: str) -> dict:
    """Honest disconnected/error card — no fabricated answer, citation, or confidence."""
    label, status_class = render.status_label(core_pb2.ERROR)
    return {
        "answer": "",
        "status_label": label,
        "status_class": status_class,
        "tier": "",
        "show_confidence": False,
        "confidence": 0.0,
        "confidence_class": "",
        "citations": [],
        "error_detail": detail,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    principal = _current_principal(request)
    ctx = {
        "oidc_enabled": auth.oidc_enabled(),
        "principal": principal.to_session() if principal else None,
    }
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/login")
async def login(request: Request):
    if not auth.oidc_enabled():
        return RedirectResponse("/")
    redirect_uri = _base_url(request) + "/auth/callback"
    return await auth.oauth().kc.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not auth.oidc_enabled():
        return RedirectResponse("/")
    try:
        token = await auth.oauth().kc.authorize_access_token(request)
    except Exception:
        logger.exception("OIDC callback failed")
        return RedirectResponse("/?auth=failed")
    claims = token.get("userinfo") or {}
    if not (claims.get("preferred_username") or claims.get("sub")):
        logger.warning("OIDC callback returned no usable identity claims")
        return RedirectResponse("/?auth=failed")
    principal = auth.principal_from_claims(claims)
    request.session["user"] = principal.to_session()
    return RedirectResponse("/")


@app.get("/logout")
async def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/")


def _require_groot(request: Request) -> auth.Principal | None:
    """Return the principal iff it is a logged-in `groot`, else None (caller → 403)."""
    principal = _current_principal(request)
    if principal is None or not principal.is_groot:
        return None
    return principal


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse(
            '<main class="shell"><article class="card">'
            '<span class="badge status-error">forbidden</span>'
            '<p class="muted">This page is groot-only.</p></article></main>',
            status_code=403,
        )
    users = await kc_admin.list_users()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"principal": principal.to_session(), "users": users, "groups": auth.known_groups()},
    )


@app.post("/admin/invite")
async def admin_invite(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    group: str = Form(""),
):
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    uname, grp = username.strip(), group.strip()
    if not uname or not password:
        return RedirectResponse("/admin", status_code=303)
    # Allowlist the group: only groups the channel maps to a scope may be assigned —
    # never let groot grant an arbitrary Keycloak group (council: codex).
    if grp and grp not in auth.known_groups():
        logger.warning("groot %s tried to assign unknown group=%s", principal.viewer, grp)
        return HTMLResponse(
            '<main class="shell"><article class="card">'
            '<span class="badge status-error">unknown group</span>'
            '<p class="muted">That group is not in the scope map. '
            '<a href="/admin">back</a></p></article></main>',
            status_code=400,
        )
    # Audit every groot grant (no secrets logged).
    logger.info("groot %s invites user=%s group=%s", principal.viewer, uname, grp or "(none)")
    try:
        await kc_admin.invite_user(uname, password, grp or None)
    except Exception:
        logger.exception("groot invite failed for user=%s", uname)
        return HTMLResponse(
            '<main class="shell"><article class="card">'
            '<span class="badge status-error">invite failed</span>'
            '<p class="muted">See server logs. <a href="/admin">back</a></p></article></main>',
            status_code=502,
        )
    return RedirectResponse("/admin", status_code=303)


@app.post("/ask", response_class=HTMLResponse)
async def ask(request: Request, q: str = Form(...)) -> HTMLResponse:
    # The HTML `required` is client-only and bypassable; don't spend an Ask on an
    # empty/whitespace query — just clear the answer region.
    if not q.strip():
        return HTMLResponse("")

    if auth.oidc_enabled():
        principal = _current_principal(request)
        if principal is None:
            # Session ended / never authenticated — never query the kernel anonymously.
            return HTMLResponse(
                '<article class="card"><span class="badge status-warn">sign in</span>'
                '<p class="muted">Your session ended. '
                '<a href="/login">Log in</a> to ask.</p></article>'
            )
        viewer, scopes = principal.viewer, principal.scopes
    else:
        viewer, scopes = _viewer(), _scopes()

    try:
        resp = await core_client.ask(q, scopes=scopes, viewer=viewer)
        ctx = _answer_view(resp)
    except aio.AioRpcError as err:
        # Unreachable / DEADLINE_EXCEEDED / etc. — honest error with the gRPC code.
        ctx = _error_view(err.code().name)
    except Exception:
        # Never crash the page or leak internals: log server-side, show a generic error.
        logger.exception("unexpected error handling /ask")
        ctx = _error_view("internal")
    return templates.TemplateResponse(request, "_answer.html", ctx)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"
