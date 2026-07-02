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
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

import grpc
from fastapi import FastAPI, Form, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from grpc import aio
from starlette.middleware.sessions import SessionMiddleware

from web_channel import actor, auth, convlog, core_client, kc_admin, localusers, render
from web_channel._gen import core_pb2

# Friendly answer-trace: how the kernel produced the answer (from the structured
# tier — never inferred from prose). The full consilium deliberation is a later phase.
_TRACE_PATH = {
    "tier0": "answered directly (no retrieval)",
    "tier_tools": "retrieval (deterministic)",
    "escalate": "gate → consilium (multi-model)",
}

_STATUS_STR = {
    core_pb2.FOUND: "found",
    core_pb2.NOT_FOUND: "not_found",
    core_pb2.PARTIAL: "partial",
    core_pb2.ERROR: "error",
}

# String status → (label, css class) for posts rendered from the conversation log.
_STATUS_VIEW = {
    "found": ("found", "status-found"),
    "partial": ("partial", "status-warn"),
    "not_found": ("not found", "status-warn"),
    "error": ("error", "status-error"),
    "unspecified": ("unspecified", "status-warn"),
}


def _split_question(q: str) -> tuple[str, str]:
    """A microblog post: a short question is the whole post; a long one uses its
    first sentence as the title and the rest as the body."""
    q = q.strip()
    parts = re.split(r"(?<=[.!?])\s+", q, maxsplit=1)
    if len(q) > 80 and len(parts) == 2:
        return parts[0], parts[1]
    return q, ""


def _post_view(turn: dict) -> dict:
    """A conversation turn (from the log or a fresh ask) → a feed-post context."""
    title, rest = _split_question(turn["question"])
    label, status_class = _STATUS_VIEW.get(turn["status"], ("", "status-warn"))
    dur = turn.get("duration_ms")
    asked = turn.get("asked_at")
    return {
        "q_title": title,
        "q_rest": rest,
        "answer": turn["answer"],
        "status_label": label,
        "status_class": status_class,
        "tier": turn["tier"],
        "path": _TRACE_PATH.get(turn["tier"], ""),
        "show_confidence": turn["status"] in ("found", "partial"),
        "confidence": turn["confidence"],
        "confidence_class": render.confidence_class(turn["confidence"] or 0.0),
        # Never show fabricated evidence on a non-found result (honesty).
        "citations": turn["citations"] if turn["status"] in ("found", "partial") else [],
        "asked_at": datetime.fromtimestamp(asked).strftime("%Y-%m-%d %H:%M:%S") if asked else "",
        "duration": f"{dur / 1000:.1f}s" if dur else None,
        # Opaque handle to the retained deliberation (ADR-15); the post shows the
        # "see how it decided" affordance only when it is present.
        "ask_ref": turn.get("ask_ref", ""),
    }


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
    # SameSite=lax: the session cookie is NOT sent on cross-site POSTs, which
    # mitigates CSRF on the state-changing routes (/ask, /admin/*, /login/local).
    # Prod hardening: add explicit per-form CSRF tokens (council: codex).
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


def _status_view(s: core_pb2.StatusResponse) -> dict:
    """KbStatus → the 'state of my memory' tile context (all from real kernel state)."""
    return {
        "nodes": s.nodes,
        "edges": s.edges,
        "last_activity": s.last_activity or "never",
        "inventory": [{"type": tc.type, "count": tc.count} for tc in s.inventory],
        "namespaces": [
            {"namespace": n.namespace, "model": n.model, "dim": n.dim, "status": n.status}
            for n in s.namespaces
        ],
        "capabilities": list(s.capabilities),
    }


def _actor_assertion(principal: auth.Principal) -> str:
    """A fresh, short-lived signed actor assertion for this principal (ADR-16 D9),
    or "" when signing isn't possible (no `SWARM_ACTOR_SECRET` configured, or an
    incomplete identity) — callers then fall back to the legacy plaintext viewer,
    which the kernel's dual-accept mode still honors during the migration window."""
    if not principal.sub or not principal.provider:
        return ""
    return actor.sign(principal.sub, principal.provider, principal.sid or "") or ""


def _session_ctx(request: Request) -> tuple[str, list[str], str] | None:
    """(viewer, scopes, assertion) for this request, or None when OIDC is on but
    there is no session — the caller then renders an honest 'session ended', never
    querying the kernel anonymously. When OIDC is off, the fixed operator at public
    scope with no assertion (no identity to sign for in pre-auth P0 mode)."""
    if auth.oidc_enabled():
        principal = _current_principal(request)
        if principal is None:
            return None
        return (principal.viewer, principal.scopes, _actor_assertion(principal))
    return _viewer(), _scopes(), ""


async def _resolve_and_gate(
    request: Request, principal: auth.Principal
) -> tuple[auth.Principal, HTMLResponse | None]:
    """Mint this login's session id, then — when signing is configured — verify the
    actor with the kernel (ADR-16 D9) and cache its DERIVED {uuid, caps}. An honest
    'not provisioned' page blocks the session on a clean UNAUTHENTICATED verdict (no
    identity_link on record yet); a transient transport error does NOT lock the user
    out (fail open to the legacy dual-accept path — self-heals once the kernel is
    reachable, since every later call re-signs a fresh assertion locally, no RPC
    needed). When no secret is configured yet, this is a no-op (pre-6a-rollout dev)."""
    principal.sid = secrets.token_urlsafe(16)
    if not actor.secret_configured():
        return principal, None
    token = actor.sign(principal.sub, principal.provider, principal.sid)
    if token is None:
        return principal, None
    try:
        resp = await core_client.resolve_actor(token)
    except aio.AioRpcError as err:
        # Fail open ONLY on a transport-level outage (kernel down / slow) — never on
        # a code the kernel used to actively REJECT the call (council: codex+gemini,
        # both independently flagged a blanket `except` as the one fix needed here).
        if err.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
            logger.warning(
                "ResolveActor unreachable at login (sub=%s provider=%s, %s) — proceeding "
                "unverified; the kernel's dual-accept mode still applies",
                principal.sub,
                principal.provider,
                err.code().name,
            )
            return principal, None
        logger.exception(
            "ResolveActor rejected the call (sub=%s provider=%s)", principal.sub, principal.provider
        )
        return principal, HTMLResponse(
            '<article class="card"><span class="badge status-error">verification failed</span>'
            f'<p class="muted">Could not verify your identity ({err.code().name}). '
            "Try again, or contact an administrator if this persists.</p></article>",
            status_code=502,
        )
    if resp.status != core_pb2.CALL_OK:
        logger.warning(
            "ResolveActor: not provisioned (sub=%s provider=%s)", principal.sub, principal.provider
        )
        return principal, templates.TemplateResponse(
            request, "not_provisioned.html", {}, status_code=403
        )
    principal.uuid = resp.uuid
    principal.caps = list(resp.caps)
    return principal, None


def _deliberation_view(d: core_pb2.DeliberationResponse) -> dict | None:
    """Deliberation → panel-vs-judge context, or None for any non-FOUND (expired /
    not-owner / scopes-no-longer-cover) — rendered as an honest absent state, never
    an error. All fields verbatim from the typed response (presentation determinism)."""
    if d.status != core_pb2.FOUND:
        return None
    return {
        "answer": d.answer,
        "confidence": d.confidence,
        "confidence_class": render.confidence_class(d.confidence),
        # A designed indicator, not a bare float: agreement = 1 - disagreement.
        "disagreement": d.disagreement,
        "agreement_pct": max(0, min(100, round((1.0 - d.disagreement) * 100))),
        "judge": d.judge,
        "panel": [{"model": t.model, "answer": t.answer} for t in d.panel],
        "created_at": d.created_at,
    }


def _neighborhood_view(r: core_pb2.NeighborhoodResponse) -> dict | None:
    """Neighborhood → connections context, or None for NOT_FOUND (out-of-scope /
    absent center) → an honest empty state. Edges grouped by relation; the distinct
    relation set drives the link-type filter chips. All verbatim from typed fields."""
    if r.status != core_pb2.FOUND:
        return None
    nodes = [
        {"id": n.id, "type": n.type, "key": n.key, "scope": n.scope, "depth": n.depth}
        for n in r.nodes
    ]
    edges = [
        {
            "src_id": e.src_id,
            "dst_id": e.dst_id,
            "relation": e.relation,
            "reliability": e.reliability,
        }
        for e in r.edges
    ]
    relations = sorted({e["relation"] for e in edges})
    return {
        "center_id": r.center_id,
        "nodes": nodes,
        "edges": edges,
        "relations": relations,
        "truncated": r.truncated,
    }


def _activity_event_view(e: core_pb2.ActivityEvent) -> dict:
    """One typed ActivityEvent → render context (verbatim typed fields)."""
    return {
        "kind": e.kind,
        "at": e.at,
        "subject_type": e.subject_type,
        "outcome": e.outcome,
        "count": e.count,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    principal = _current_principal(request)
    # Sign-in gate — when auth is configured (OIDC and/or local users) and no session,
    # go to the unified /login (which auto-routes SSO vs local).
    if principal is None and (auth.oidc_enabled() or localusers.has_any()):
        return RedirectResponse("/login")
    # Cold open lands on the dashboard (brief A.1.1), not a blank box. The KbStatus
    # tile loads async (HTMX) so the page is instant and never blocks on the kernel.
    viewer = principal.viewer if principal else _viewer()
    try:
        recent = convlog.recent(viewer, 15)  # for the sidebar links (durable, per-viewer)
    except Exception:
        logger.exception("convlog read failed")
        recent = []
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "oidc_enabled": auth.oidc_enabled(),
            "authed": True,  # home → show the ⌘K search + palette in the header
            "principal": principal.to_session() if principal else None,
            "recent": [
                {
                    "id": t["id"],
                    "question": _split_question(t["question"])[0],
                    "status": t["status"],
                }
                for t in recent
            ],
        },
    )


@app.get("/conversation/{conv_id}", response_class=HTMLResponse)
async def conversation(request: Request, conv_id: int) -> HTMLResponse:
    """Reopen a past conversation (a Recent link) into the main answer area."""
    if auth.oidc_enabled():
        principal = _current_principal(request)
        if principal is None:
            return HTMLResponse('<p class="muted">Session ended — <a href="/login">log in</a>.</p>')
        viewer = principal.viewer
    else:
        viewer = _viewer()
    turn = convlog.get(viewer, conv_id)
    if turn is None:
        return HTMLResponse('<p class="muted">Conversation not found.</p>', status_code=404)
    return templates.TemplateResponse(request, "_post.html", {"post": _post_view(turn)})


@app.get("/tile/status", response_class=HTMLResponse)
async def tile_status(request: Request) -> HTMLResponse:
    """The 'state of my memory' tile — real KbStatus, or an honest unavailable state."""
    try:
        ctx = {"status": _status_view(await core_client.kb_status())}
    except Exception:
        logger.exception("KbStatus failed for dashboard tile")
        ctx = {"status": None}
    return templates.TemplateResponse(request, "_status_tile.html", ctx)


@app.get("/search", response_class=HTMLResponse)
async def search(request: Request, q: str = "") -> HTMLResponse:
    """⌘K command palette: scope-filtered KbSearch → hit list (keyboard-first)."""
    q = q.strip()
    if not q:
        return HTMLResponse("")
    if auth.oidc_enabled():
        principal = _current_principal(request)
        if principal is None:
            return HTMLResponse(
                '<li class="muted">session ended — <a href="/login">log in</a></li>'
            )
        scopes, assertion = principal.scopes, _actor_assertion(principal)
    else:
        scopes, assertion = _scopes(), ""
    try:
        resp = await core_client.kb_search(q, scopes=scopes, limit=10, assertion=assertion)
        # id is the bridge search → graph: a hit opens its Neighborhood (ADR-15).
        hits = [{"id": h.id, "type": h.type, "key": h.key, "score": h.score} for h in resp.hits]
        ctx = {"hits": hits, "q": q}
    except Exception:
        logger.exception("KbSearch failed")
        ctx = {"hits": None, "q": q}
    return templates.TemplateResponse(request, "_hits.html", ctx)


@app.get("/deliberation/{ask_ref}", response_class=HTMLResponse)
async def deliberation(request: Request, ask_ref: str) -> HTMLResponse:
    """The panel-vs-judge deliberation behind an escalated answer (ADR-15), opened
    from a post's affordance. Viewer+scopes from the session; the kernel re-auths."""
    ctx = _session_ctx(request)
    if ctx is None:
        return HTMLResponse('<p class="muted">Session ended — <a href="/login">log in</a>.</p>')
    viewer, scopes, assertion = ctx
    try:
        resp = await core_client.deliberation(ask_ref, scopes=scopes, viewer=assertion or viewer)
        delib = _deliberation_view(resp)
    except Exception:
        logger.exception("Deliberation failed")
        delib = None
    return templates.TemplateResponse(request, "_deliberation.html", {"delib": delib})


@app.get("/neighborhood/{node_id}", response_class=HTMLResponse)
async def neighborhood(request: Request, node_id: int, rel: str = "") -> HTMLResponse:
    """The bounded, scope-filtered connections around a node (ADR-15), opened from a
    ⌘K hit. `rel` is an optional comma-separated relation-type filter."""
    ctx = _session_ctx(request)
    if ctx is None:
        return HTMLResponse('<p class="muted">Session ended — <a href="/login">log in</a>.</p>')
    viewer, scopes, assertion = ctx
    relation_types = [r for r in (rel.split(",") if rel else []) if r.strip()]
    try:
        resp = await core_client.neighborhood(
            node_id,
            scopes=scopes,
            viewer=assertion or viewer,
            depth=1,
            relation_types=relation_types,
        )
        view = _neighborhood_view(resp)
    except Exception:
        logger.exception("Neighborhood failed")
        view = None
    return templates.TemplateResponse(
        request,
        "_neighborhood.html",
        {"hood": view, "center_id": node_id, "active_rel": rel},
    )


@app.get("/activity", response_class=HTMLResponse)
async def activity(request: Request, cursor: str = "") -> HTMLResponse:
    """One poll of the scope-safe ActivityFeed (ADR-15). Returns the new events plus
    an out-of-band poller carrying the opaque `next_cursor` for the next tick. On a
    dead session, returns a static message WITHOUT a poller, so polling stops."""
    ctx = _session_ctx(request)
    if ctx is None:
        # Disarm the poller (OOB, no trigger) so the loop actually stops, then notify.
        return HTMLResponse(
            '<li class="muted">Session ended — <a href="/login">log in</a>.</li>'
            '<span id="activity-poller" hx-swap-oob="true"></span>'
        )
    viewer, scopes, assertion = ctx
    events: list[dict] = []
    next_cursor = cursor
    try:
        resp = await core_client.activity_feed(
            scopes=scopes, viewer=assertion or viewer, cursor=cursor, limit=25
        )
        events = [_activity_event_view(e) for e in resp.events]
        next_cursor = resp.next_cursor
    except Exception:
        logger.exception("ActivityFeed failed")  # keep polling at the same cursor
    return templates.TemplateResponse(
        request, "_activity.html", {"events": events, "next_cursor": next_cursor}
    )


@app.get("/history", response_class=HTMLResponse)
async def history(request: Request) -> Response:
    """Self-serve kernel-owned conversation list (ADR-16 D9) — List over the actor's
    OWN conversations, the kernel-authoritative record (the same one the admin
    break-glass path reads). The richer per-turn trace (tier/confidence/citations)
    still lives in the local convlog (home's Recent) — the kernel's Conversation
    model doesn't carry it; this view is honest about that, not a replacement."""
    principal = _current_principal(request)
    if principal is None and (auth.oidc_enabled() or localusers.has_any()):
        return RedirectResponse("/login")
    assertion = _actor_assertion(principal) if principal else ""
    conversations: list[dict] | None = None
    if assertion:
        try:
            resp = await core_client.list_conversations(assertion)
            if resp.status == core_pb2.CALL_OK:
                conversations = [
                    {"id": c.id, "title": c.title, "created_at": c.created_at}
                    for c in resp.conversations
                ]
        except Exception:
            logger.exception("ListConversations failed")
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "authed": True,
            "principal": principal.to_session() if principal else None,
            "conversations": conversations,
            "signed": bool(assertion),
        },
    )


@app.get("/history/{conv_id}", response_class=HTMLResponse)
async def history_thread(request: Request, conv_id: str) -> Response:
    """One kernel-owned conversation's raw turns (owner-gated; 404-not-403)."""
    principal = _current_principal(request)
    if principal is None and (auth.oidc_enabled() or localusers.has_any()):
        return RedirectResponse("/login")
    assertion = _actor_assertion(principal) if principal else ""
    conversation: dict | None = None
    messages: list[dict] | None = None
    if assertion:
        try:
            resp = await core_client.get_conversation(assertion, conv_id)
            if resp.status == core_pb2.CALL_OK:
                conversation = {"title": resp.conversation.title}
                messages = [
                    {
                        "role": m.role,
                        "body": m.body,
                        "ask_ref": m.ask_ref,
                        "created_at": m.created_at,
                    }
                    for m in resp.messages
                ]
        except Exception:
            logger.exception("GetConversation failed")
    return templates.TemplateResponse(
        request,
        "history_thread.html",
        {
            "authed": True,
            "principal": principal.to_session() if principal else None,
            "conversation": conversation,
            "messages": messages,
        },
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> Response:
    """The observability page: full stats + activity + the connections graph.
    Same sign-in gate as home; the channel never reads the DB (RPCs only)."""
    principal = _current_principal(request)
    if principal is None and (auth.oidc_enabled() or localusers.has_any()):
        return RedirectResponse("/login")
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "authed": True,
            "principal": principal.to_session() if principal else None,
        },
    )


@app.get("/dashboard/search")
async def dashboard_search(request: Request, q: str = "") -> JSONResponse:
    """Scope-filtered KbSearch as JSON — the graph explorer's entity picker. Returns
    SearchHit.id (the bridge to Neighborhood). Honest empty on no session/error."""
    q = q.strip()
    ctx = _session_ctx(request)
    if not q or ctx is None:
        return JSONResponse({"hits": []})
    _viewer_id, scopes, assertion = ctx
    try:
        resp = await core_client.kb_search(q, scopes=scopes, limit=10, assertion=assertion)
        hits = [{"id": h.id, "type": h.type, "key": h.key, "score": h.score} for h in resp.hits]
    except Exception:
        logger.exception("dashboard KbSearch failed")
        hits = []
    return JSONResponse({"hits": hits})


@app.get("/dashboard/graph/{node_id}")
async def dashboard_graph(request: Request, node_id: int, rel: str = "") -> JSONResponse:
    """A bounded, scope-filtered neighborhood as JSON for the Cytoscape graph (ADR-15).
    Kernel enforces scope (Neighborhood RPC); verbatim ids/keys/relations, no DB reads."""
    ctx = _session_ctx(request)
    if ctx is None:
        return JSONResponse({"status": "not_found", "nodes": [], "edges": []}, status_code=401)
    viewer, scopes, assertion = ctx
    relation_types = [r for r in (rel.split(",") if rel else []) if r.strip()]
    try:
        resp = await core_client.neighborhood(
            node_id,
            scopes=scopes,
            viewer=assertion or viewer,
            depth=1,
            relation_types=relation_types,
        )
        view = _neighborhood_view(resp)
    except Exception:
        logger.exception("dashboard Neighborhood failed")
        return JSONResponse({"status": "error", "nodes": [], "edges": []})
    if view is None:
        return JSONResponse({"status": "not_found", "center_id": node_id, "nodes": [], "edges": []})
    return JSONResponse({"status": "found", **view})


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    """Unified entry: one identifier field. Continue auto-routes SSO vs local."""
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
async def login_route(request: Request, identifier: str = Form(...)):
    """Auto-route by identifier: a known LOCAL user → the local password form;
    otherwise → Keycloak SSO (with the identifier prefilled). Local users never
    touch Keycloak; SSO users never see a local password box."""
    ident = identifier.strip()
    if not ident:
        return templates.TemplateResponse(request, "login.html", {"error": "Enter a username."})
    if localusers.exists(ident):
        return templates.TemplateResponse(request, "login_local.html", {"identifier": ident})
    if auth.oidc_enabled():
        redirect_uri = _base_url(request) + "/auth/callback"
        return await auth.oauth().kc.authorize_redirect(request, redirect_uri, login_hint=ident)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Unknown user, and SSO is not configured."}
    )


@app.post("/login/local")
async def login_local(request: Request, identifier: str = Form(...), password: str = Form(...)):
    """Verify a LOCAL user against the channel's own credential store."""
    ident = identifier.strip()
    principal = localusers.verify(ident, password)
    if principal is None:
        logger.warning("failed local login for %s", ident)
        return templates.TemplateResponse(
            request,
            "login_local.html",
            {"identifier": ident, "error": "Invalid credentials."},
            status_code=401,
        )
    principal, blocked = await _resolve_and_gate(request, principal)
    if blocked is not None:
        return blocked
    request.session["user"] = principal.to_session()
    return RedirectResponse("/", status_code=303)


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
    principal, blocked = await _resolve_and_gate(request, principal)
    if blocked is not None:
        return blocked
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
            '<span class="badge status-error">forbidden</span></article></main>',
            status_code=403,
        )
    try:
        users = await kc_admin.list_users()
    except Exception:
        logger.exception("Keycloak list_users failed")
        users = None  # template shows an honest "Keycloak unavailable"
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "authed": True,
            "principal": principal.to_session(),
            "users": users,
            "local_users": localusers.list_users(),
            "groups": auth.known_groups(),
        },
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


@app.post("/admin/local-invite")
async def admin_local_invite(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    group: str = Form(""),
):
    """Create a LOCAL (non-SSO) user in the channel's own store. groot-gated."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    uname, grp = username.strip(), group.strip()
    if not uname or not password:
        return RedirectResponse("/admin", status_code=303)
    if grp and grp not in auth.known_groups():
        return HTMLResponse(
            '<main class="shell"><article class="card">'
            '<span class="badge status-error">unknown group</span>'
            '<p class="muted">That group is not in the scope map. '
            '<a href="/admin">back</a></p></article></main>',
            status_code=400,
        )
    scopes = [auth.scopes_for([grp])[-1]] if grp else []  # the mapped scope for the group
    logger.info("groot %s creates LOCAL user=%s group=%s", principal.viewer, uname, grp or "(none)")
    try:
        localusers.create(uname, password, scopes, is_groot=False, created_by=principal.viewer)
    except ValueError:
        return HTMLResponse(
            '<main class="shell"><article class="card">'
            '<span class="badge status-error">exists</span>'
            '<p class="muted">A local user with that name already exists. '
            '<a href="/admin">back</a></p></article></main>',
            status_code=409,
        )
    return RedirectResponse("/admin", status_code=303)


# --- Kernel-backed admin (ADR-16 D9/D10/D11, step 6b.4) --------------------
# The kernel is the sole authority: every call below is capability-gated and
# audited SERVER-SIDE (Swarm.Admin/Swarm.Conversations); the channel only signs
# the assertion and renders the typed CallStatus honestly. Fixed-label error
# pages only (never interpolate a form value into a raw HTML string — autoescape
# via Jinja is used wherever kernel/user content is actually rendered, below).

_CALL_STATUS_LABEL = {
    core_pb2.CALL_NOT_FOUND: ("not found", "status-warn"),
    core_pb2.CALL_UNAUTHENTICATED: ("not signed in to the kernel", "status-error"),
    core_pb2.CALL_NOT_AUTHORIZED: ("not authorized", "status-error"),
    core_pb2.CALL_BAD_REQUEST: ("bad request", "status-error"),
}


def _admin_outcome_page(status_code: int, label: str, css_class: str) -> HTMLResponse:
    return HTMLResponse(
        '<main class="shell"><article class="card">'
        f'<span class="badge {css_class}">{label}</span>'
        '<p class="muted">The kernel rejected this action — see above.</p>'
        '<p><a class="navlink" href="/admin">back to admin</a></p></article></main>',
        status_code=status_code,
    )


def _admin_assertion(principal: auth.Principal) -> str:
    """The signed assertion for a kernel-backed admin action, or "" when signing
    isn't configured — callers then render an honest 'kernel identity not
    resolved' outcome (CALL_UNAUTHENTICATED) rather than attempting the call."""
    return _actor_assertion(principal)


@app.post("/admin/kernel/user")
async def admin_kernel_user(
    request: Request,
    op: str = Form(...),
    target_user_id: str = Form(""),
    login: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    nickname: str = Form(""),
    password: str = Form(""),
    group: str = Form(""),
):
    """ManageUser (invite/deactivate/delete). INVITE also provisions a channel-local
    credential in the SAME action, so the invited user can sign in immediately
    (`Swarm.Identity.invite_user` alone leaves `status='invited'`, and no gRPC path
    yet promotes it to 'active' — `board/todo/jit-provision-rpc` — so the FIRST
    local login must already resolve; that gap is tracked, not silently papered over)."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    assertion = _admin_assertion(principal)
    op_map = {
        "invite": core_pb2.INVITE,
        "deactivate": core_pb2.DEACTIVATE,
        "delete": core_pb2.DELETE,
    }
    if op not in op_map:
        return _admin_outcome_page(400, "bad request", "status-error")
    try:
        resp = await core_client.manage_user(
            assertion,
            op_map[op],
            target_user_id=target_user_id.strip(),
            login=login.strip(),
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            nickname=nickname.strip(),
        )
    except Exception:
        logger.exception("ManageUser failed")
        return _admin_outcome_page(502, "kernel unreachable", "status-error")
    if resp.status != core_pb2.CALL_OK:
        label, css_class = _CALL_STATUS_LABEL.get(resp.status, ("error", "status-error"))
        return _admin_outcome_page(409, label, css_class)
    if op == "invite" and login.strip() and password:
        # Pair the kernel identity with a channel-local credential (best-effort —
        # the kernel record already exists even if this half fails; groot can
        # retry via the plain local-invite form above).
        try:
            grp = group.strip()
            scopes = [auth.scopes_for([grp])[-1]] if grp else []
            localusers.create(
                login.strip(), password, scopes, is_groot=False, created_by=principal.viewer
            )
        except ValueError:
            logger.warning("kernel-invited login=%s already has a local credential", login)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/kernel/access")
async def admin_kernel_access(
    request: Request,
    op: str = Form(...),
    target_user_id: str = Form(""),
    role: str = Form(""),
    group_id: str = Form(""),
    scopes: str = Form(""),
):
    """ManageAccess (grant/revoke role or group; set a group's scopes) — superadmin
    for role ops, `manage_access` cap for group ops (kernel-enforced, not here)."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    assertion = _admin_assertion(principal)
    op_map = {
        "grant_role": core_pb2.GRANT_ROLE,
        "revoke_role": core_pb2.REVOKE_ROLE,
        "grant_group": core_pb2.GRANT_GROUP,
        "revoke_group": core_pb2.REVOKE_GROUP,
        "set_group_scopes": core_pb2.SET_GROUP_SCOPES,
    }
    if op not in op_map:
        return _admin_outcome_page(400, "bad request", "status-error")
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    try:
        resp = await core_client.manage_access(
            assertion,
            op_map[op],
            target_user_id=target_user_id.strip(),
            role=role.strip(),
            group_id=group_id.strip(),
            scopes=scope_list,
        )
    except Exception:
        logger.exception("ManageAccess failed")
        return _admin_outcome_page(502, "kernel unreachable", "status-error")
    if resp.status != core_pb2.CALL_OK:
        label, css_class = _CALL_STATUS_LABEL.get(resp.status, ("error", "status-error"))
        return _admin_outcome_page(409, label, css_class)
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/kernel/read-conversation", response_class=HTMLResponse)
async def admin_kernel_read_conversation(
    request: Request, conversation_id: str = Form(...), reason: str = Form(...)
) -> HTMLResponse:
    """Break-glass (AdminReadConversation, D6): superadmin + `read_any_conversation`
    only; the kernel audits BEFORE returning (Swarm.Audit) — reason is required at
    the wire boundary (an empty reason is CALL_BAD_REQUEST, not an unlogged read).
    Renders the audited fact visibly: the channel itself sent `reason` on this
    exact request, and the kernel's contract guarantees it logged it first."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not reason.strip():
        return _admin_outcome_page(400, "reason is required", "status-error")
    assertion = _admin_assertion(principal)
    try:
        resp = await core_client.admin_read_conversation(
            assertion, conversation_id.strip(), reason.strip()
        )
    except Exception:
        logger.exception("AdminReadConversation failed")
        return _admin_outcome_page(502, "kernel unreachable", "status-error")
    if resp.status != core_pb2.CALL_OK:
        label, css_class = _CALL_STATUS_LABEL.get(resp.status, ("error", "status-error"))
        return _admin_outcome_page(
            404 if resp.status == core_pb2.CALL_NOT_FOUND else 409, label, css_class
        )
    return templates.TemplateResponse(
        request,
        "admin_read_conversation.html",
        {
            "authed": True,
            "principal": principal.to_session(),
            "actor_login": principal.viewer,
            "reason": reason.strip(),
            "conversation": {
                "title": resp.conversation.title,
                "owner_id": resp.conversation.owner_id,
            },
            "messages": [
                {"role": m.role, "body": m.body, "ask_ref": m.ask_ref, "created_at": m.created_at}
                for m in resp.messages
            ],
        },
    )


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
        viewer, scopes, assertion = principal.viewer, principal.scopes, _actor_assertion(principal)
    else:
        viewer, scopes, assertion = _viewer(), _scopes(), ""

    qs = q.strip()
    asked_at = time.time()
    started = time.monotonic()
    answer_text, tier, status_str, conf, cites, ask_ref = "", "error", "error", 0.0, [], ""
    try:
        resp = await core_client.ask(q, scopes=scopes, viewer=assertion or viewer)
        answer_text, tier, conf = resp.answer, resp.tier, resp.confidence
        status_str = _STATUS_STR.get(resp.status, "unspecified")
        ask_ref = resp.ask_ref  # opaque deliberation handle; "" unless escalated (ADR-15)
        cites = [
            {"source": c.source, "ref": c.ref, "confidence": c.confidence} for c in resp.citations
        ]
    except aio.AioRpcError as err:
        # Unreachable / DEADLINE_EXCEEDED / etc. — honest error with the gRPC code.
        answer_text = f"Could not reach the knowledge base ({err.code().name})."
    except Exception:
        # Never crash the page or leak internals: log server-side, show a generic error.
        logger.exception("unexpected error handling /ask")
        answer_text = "Something went wrong handling this question."
    duration_ms = int((time.monotonic() - started) * 1000)

    # Durable per-viewer conversation log (best-effort — must never break /ask).
    try:
        convlog.log_turn(
            viewer,
            scopes,
            qs,
            answer_text,
            tier,
            status_str,
            conf,
            cites,
            asked_at=asked_at,
            duration_ms=duration_ms,
            ask_ref=ask_ref,
        )
    except Exception:
        logger.exception("convlog write failed")

    # Dual-write to the kernel-owned, owner-enforced conversation store (ADR-16
    # D9/step 6b) — best-effort, must never break /ask. The LOCAL convlog above
    # remains the primary read path (it carries the answer trace — tier/confidence/
    # citations — the kernel's Conversation model doesn't); this is what makes the
    # per-user privacy invariant load-bearing end-to-end rather than dormant. Only
    # possible once a signed identity exists (assertion non-empty).
    if assertion:
        try:
            title, _ = _split_question(qs)
            user_msg = await core_client.log_conversation(
                assertion, title=title, role="user", body=qs
            )
            if user_msg.status == core_pb2.CALL_OK:
                await core_client.log_conversation(
                    assertion,
                    conversation_id=user_msg.conversation_id,
                    role="assistant",
                    body=answer_text,
                    ask_ref=ask_ref,
                )
        except Exception:
            logger.exception("kernel LogConversation failed (convlog remains the record)")

    turn = {
        "question": qs,
        "answer": answer_text,
        "tier": tier,
        "status": status_str,
        "confidence": conf,
        "citations": cites,
        "asked_at": asked_at,
        "duration_ms": duration_ms,
        "ask_ref": ask_ref,
    }
    # A new post for the feed (HTMX prepends it).
    return templates.TemplateResponse(request, "_post.html", {"post": _post_view(turn)})


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"
