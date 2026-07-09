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

import asyncio
import logging
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

import grpc
import httpx
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

from web_channel import actor, auth, convlog, core_client, kc_admin, localusers, render, settings
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


# A post title must fit the aside's history list — longer first sentences are
# display-truncated (the body keeps the full text, nothing is lost).
_TITLE_MAX = 90


def _split_question(q: str) -> tuple[str, str]:
    """A post's (title, body) — microblog rules (operator, 2026-07-08):
    an explicit `# Heading` line IS the title; else the first sentence OR first
    line (whichever ends sooner) is; a short post is all title; an overlong first
    sentence is display-truncated while the body keeps the full text."""
    q = q.strip()
    # 1.3 — an explicit markdown heading names the topic.
    m = re.match(r"^#{1,3}\s+(.+?)\s*\n+(.*)$", q, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    if m2 := re.match(r"^#{1,3}\s+(.+?)\s*$", q):  # heading-only post
        return m2.group(1).strip(), ""
    # 1.2 — a line break is an explicit structure signal: the first line is the
    # title regardless of total length (a multi-paragraph post is never all-title).
    if "\n" in q:
        first, rest = q.split("\n", 1)
        title, rest = first.strip(), rest.strip()
    else:
        # 1.1 — single line: a short post is all title (microblog); a long one
        # takes its first sentence.
        parts = re.split(r"(?<=[.!?])\s+", q, maxsplit=1)
        if len(q) > 80 and len(parts) == 2:
            title, rest = parts[0].strip(), parts[1].strip()
        else:
            title, rest = q, ""
    if len(title) > _TITLE_MAX:
        return title[: _TITLE_MAX - 1].rsplit(" ", 1)[0] + "…", q
    return title, rest


def _post_view(turn: dict) -> dict:
    """A conversation turn (from the log or a fresh ask) → a feed-post context."""
    title, rest = _split_question(turn["question"])
    label, status_class = _STATUS_VIEW.get(turn["status"], ("", "status-warn"))
    dur = turn.get("duration_ms")
    asked = turn.get("asked_at")
    return {
        # The turn's own row id — every reply is a first-class POST with an identity
        # (an addressable anchor now; a graph-referenceable object later).
        "id": turn.get("id"),
        "q_title": title,
        # Question body + answer render as chat-style markdown (Mattermost/Slack
        # shape): lists/emphasis work, bare URLs autolink, raw HTML is escaped
        # (render.markdown, html=False) — so these are safe for |safe.
        "q_rest_html": render.markdown(rest) if rest else "",
        "question_html": render.markdown(turn["question"]),
        "answer": turn["answer"],
        "answer_html": render.markdown(turn["answer"]) if turn["answer"] else "",
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


def _active_keys_of(turn: dict | None) -> list[str]:
    """Entity keys from one turn's citations (chat-thread epic 2) — Citation.ref for
    a non-"claim" citation IS the graph node's key (kernel: `Swarm.Core.cite/1`).
    Lets a pronoun follow-up ("its dependencies?") in a post's REPLY THREAD still
    resolve on the kernel's fast structured path. Context is per-thread (post-objects
    rework): a NEW post is a new topic and gets no keys at all."""
    if not turn:
        return []
    return [c["ref"] for c in turn["citations"] if c.get("source") != "claim" and c.get("ref")]


def _post_object_view(viewer: str, root: dict) -> dict:
    """A root turn → the full post-object context: the post view + its thread handle
    + its replies (oldest first, forum order)."""
    view = _post_view(root)
    view["thread_id"] = root["id"]
    try:
        view["replies"] = [_post_view(r) for r in convlog.replies(viewer, root["id"])]
    except Exception:
        logger.exception("convlog replies read failed")
        view["replies"] = []
    return view


def _recent_titled(viewer: str) -> list[dict]:
    """Sidebar history entries: root posts, newest first, each with its display
    TITLE (same rules as the post header — a `# Heading` post shows its heading,
    not the raw markdown)."""
    try:
        recent = convlog.recent(viewer, 20)
    except Exception:
        logger.exception("convlog read failed")
        return []
    for r in recent:
        r["title"] = _split_question(r["question"])[0]
    return recent


async def _backfill_thread(assertion: str, viewer: str, root: dict) -> str:
    """Write a thread's EXISTING turns (root Q&A + replies, oldest first) into a new
    kernel conversation and bind it to the root — so a reply on a thread that predates
    kernel threading still gets real history in its Ask. Returns the conversation id,
    or "" on any failure (best-effort: the reply then proceeds with keys only)."""
    try:
        turns = [root, *convlog.replies(viewer, root["id"])]
        title, _ = _split_question(root["question"])
        conv_id = ""
        for t in turns:
            user_msg = await core_client.log_conversation(
                assertion, conversation_id=conv_id, title=title, role="user", body=t["question"]
            )
            if user_msg.status != core_pb2.CALL_OK:
                return ""
            conv_id = conv_id or user_msg.conversation_id
            await core_client.log_conversation(
                assertion,
                conversation_id=conv_id,
                role="assistant",
                body=t["answer"],
                ask_ref=t.get("ask_ref", ""),
            )
        if conv_id:
            convlog.set_kernel_conv(viewer, root["id"], conv_id)
        return conv_id
    except Exception:
        logger.exception("thread backfill into the kernel conversation failed")
        return ""


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
        k
        for k in ("OIDC_ISSUER", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET")
        if not settings.get_or_env(k)
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
    if not data:
        return None
    # A pre-6b session lacks sub/provider: it cannot sign an actor assertion, so
    # under the kernel's :strict mode it silently degrades to anonymous/public.
    # Force re-auth instead (auth-hardening card) — clear it and land on /login.
    if not (data.get("sub") and data.get("provider")):
        request.session.pop("user", None)
        return None
    return auth.Principal.from_session(data)


def _csrf_token(request: Request) -> str:
    """The session-bound CSRF token (synchronizer pattern), minted on first use.
    Embedded as a hidden `csrf` field in every admin form; an attacker page can
    submit a cross-origin POST but cannot READ this token."""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def _csrf_ok(request: Request, token: str) -> bool:
    expected = request.session.get("csrf", "")
    return bool(expected) and bool(token) and secrets.compare_digest(expected, token)


def _csrf_reject() -> HTMLResponse:
    return HTMLResponse(
        '<main class="shell"><article class="card">'
        '<span class="badge status-error">rejected</span>'
        '<p class="muted">Missing or stale form token — reload '
        '<a href="/admin">the admin page</a> and retry.</p></article></main>',
        status_code=403,
    )


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
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "oidc_enabled": auth.oidc_enabled(),
            "authed": True,  # home → show the ⌘K search + palette in the header
            "principal": principal.to_session() if principal else None,
            "recent": _recent_titled(viewer),
        },
    )


@app.get("/p/{slug}", response_class=HTMLResponse)
async def post_page(request: Request, slug: str) -> Response:
    """A post's own PAGE (its permalink — the sidebar links here, and a fresh ask
    pushes this URL). The full home layout with the post + its thread loaded; a
    reply's slug resolves to its root's page. Viewer-scoped: someone else's slug is
    indistinguishable from an absent one."""
    principal = _current_principal(request)
    if principal is None and (auth.oidc_enabled() or localusers.has_any()):
        return RedirectResponse("/login")
    viewer = principal.viewer if principal else _viewer()
    turn = convlog.get_by_slug(viewer, slug)
    if turn is not None and turn.get("thread_id"):
        turn = convlog.get(viewer, turn["thread_id"])
    if turn is None:
        return HTMLResponse('<p class="muted">Post not found.</p>', status_code=404)
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "oidc_enabled": auth.oidc_enabled(),
            "authed": True,
            "principal": principal.to_session() if principal else None,
            "recent": _recent_titled(viewer),
            "post": _post_object_view(viewer, turn),
        },
    )


@app.get("/conversation/{conv_id}")
async def conversation(request: Request, conv_id: int) -> Response:
    """Legacy integer-id link → the post's permalink page (kept for old bookmarks)."""
    if auth.oidc_enabled():
        principal = _current_principal(request)
        if principal is None:
            return RedirectResponse("/login")
        viewer = principal.viewer
    else:
        viewer = _viewer()
    turn = convlog.get(viewer, conv_id)
    if turn is None or not turn.get("slug"):
        return HTMLResponse('<p class="muted">Conversation not found.</p>', status_code=404)
    return RedirectResponse(f"/p/{turn['slug']}")


def _glances_addr() -> str:
    return os.environ.get("GLANCES_ADDR", "")


async def _glances_json(client, path: str):
    r = await client.get(f"{_glances_addr()}{path}")
    r.raise_for_status()
    return r.json()


@app.get("/tile/system", response_class=HTMLResponse)
async def tile_system(request: Request) -> HTMLResponse:
    """The footer's body-telemetry strip — CPU/RAM/load/processes (+GPU when NVML
    sees one) from the glances sidecar's REST API. Honest empty state when the
    sidecar is unconfigured/unreachable; never blocks or breaks a page."""
    if not _glances_addr():
        return HTMLResponse("")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            quick, mem, load, pcount, gpus = await asyncio.gather(
                _glances_json(client, "/api/4/quicklook"),
                _glances_json(client, "/api/4/mem"),
                _glances_json(client, "/api/4/load"),
                _glances_json(client, "/api/4/processcount"),
                _glances_json(client, "/api/4/gpu"),
            )
    except Exception:
        logger.exception("glances poll failed")
        return HTMLResponse('<span class="muted">body telemetry offline</span>')
    gib = 1024**3
    ctx = {
        "cpu": round(quick.get("cpu", 0)),
        "mem": round(quick.get("mem", 0)),
        "mem_used": f"{mem.get('used', 0) / gib:.1f}",
        "mem_total": f"{mem.get('total', 0) / gib:.0f}",
        "load1": f"{load.get('min1', 0):.2f}",
        "procs": pcount.get("total", 0),
        "running": pcount.get("running", 0),
        # GB10 unified memory: NVML reports mem as null — omit vram then, never 0%.
        "gpus": [
            {
                "name": g.get("name", "gpu"),
                "proc": round(g.get("proc") or 0),
                "mem": round(g["mem"]) if g.get("mem") is not None else None,
                "temp": round(g["temperature"]) if g.get("temperature") is not None else None,
            }
            for g in (gpus or [])
        ],
    }
    return templates.TemplateResponse(request, "_system_tile.html", ctx)


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
    # JIT-provision / re-sync BEFORE resolve (ADR-16 D3): a NEW SSO subject gets a
    # kernel identity on first login; an existing one gets their groups re-synced
    # (an IdP group removal must propagate — council gemini). Best-effort: the
    # resolve gate right after remains the access decision.
    await _provision_kernel_identity(claims, principal)
    principal, blocked = await _resolve_and_gate(request, principal)
    if blocked is not None:
        return blocked
    request.session["user"] = principal.to_session()
    # Kept for RP-initiated logout (id_token_hint) — without ending the Keycloak
    # SSO session, the next /login silently re-authenticates the SAME user.
    request.session["id_token"] = token.get("id_token") or ""
    return RedirectResponse("/")


async def _provision_kernel_identity(claims: dict, principal: auth.Principal) -> None:
    """Sign a provision token from the verified OIDC claims and call
    `ProvisionActor`. No-op when signing isn't configured. A login collision
    (CALL_BAD_REQUEST) or a disabled account (CALL_UNAUTHENTICATED) is logged and
    left to the resolve gate to render honestly — never a 500 at login."""
    token = actor.sign_provision(
        sub=principal.sub,
        provider=principal.provider,
        login=principal.viewer,
        groups=principal.groups,
        first_name=str(claims.get("given_name") or ""),
        last_name=str(claims.get("family_name") or ""),
        nickname=str(claims.get("nickname") or ""),
        email=str(claims.get("email") or ""),
    )
    if not token:
        return
    try:
        resp = await core_client.provision_actor(token)
    except Exception:
        logger.warning("ProvisionActor unreachable — proceeding to the resolve gate")
        return
    if resp.status == core_pb2.CALL_BAD_REQUEST:
        logger.warning(
            "ProvisionActor: login collision for sub=%s (admin must link/rename)",
            principal.sub,
        )
    elif resp.status == core_pb2.CALL_UNAUTHENTICATED:
        logger.info("ProvisionActor: refused (disabled account or signing mismatch)")


def _end_session_url(session: dict, base_url: str) -> str:
    """The Keycloak RP-initiated-logout URL: ends the IdP's SSO session, not just
    ours. With an `id_token_hint` the logout is silent; without one Keycloak may
    show a confirm screen (still correct — the SSO cookie dies either way).
    Observed live before this fix: every /auth/callback carried the SAME
    session_state, so 'logging in as bob' silently returned alice."""
    issuer = settings.get_or_env("OIDC_ISSUER").rstrip("/")
    from urllib.parse import urlencode

    params = {
        "post_logout_redirect_uri": base_url,
        "client_id": settings.get_or_env("OIDC_CLIENT_ID", ""),
    }
    id_token = session.get("id_token") or ""
    if id_token:
        params["id_token_hint"] = id_token
    return f"{issuer}/protocol/openid-connect/logout?{urlencode(params)}"


@app.get("/logout")
async def logout(request: Request):
    session_snapshot = dict(request.session)
    request.session.pop("user", None)
    request.session.pop("id_token", None)
    if auth.oidc_enabled():
        # End the Keycloak SSO session too — else the next /login silently
        # re-authenticates the same IdP user regardless of intent.
        return RedirectResponse(_end_session_url(session_snapshot, _base_url(request)))
    return RedirectResponse("/")


def _require_groot(request: Request) -> auth.Principal | None:
    """Return the principal iff it is a logged-in `groot`, else None (caller → 403)."""
    principal = _current_principal(request)
    if principal is None or not principal.is_groot:
        return None
    return principal


_CONNECTOR_KEYS = (
    "OIDC_ISSUER",
    "KEYCLOAK_REALM",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "KEYCLOAK_ADMIN_URL",
    "KEYCLOAK_ADMIN_USER",
    "KEYCLOAK_ADMIN_PASSWORD",
)


def _connector_view() -> dict[str, str]:
    return {
        "issuer": settings.get_or_env("OIDC_ISSUER"),
        "realm": settings.get_or_env("KEYCLOAK_REALM", "swarm-local"),
        "client_id": settings.get_or_env("OIDC_CLIENT_ID"),
        "admin_url": settings.get_or_env("KEYCLOAK_ADMIN_URL", "http://keycloak:8080"),
        "admin_user": settings.get_or_env("KEYCLOAK_ADMIN_USER", "admin"),
    }


def _connector_values() -> dict[str, str]:
    return {
        "OIDC_ISSUER": settings.get_or_env("OIDC_ISSUER"),
        "KEYCLOAK_REALM": settings.get_or_env("KEYCLOAK_REALM", "swarm-local"),
        "OIDC_CLIENT_ID": settings.get_or_env("OIDC_CLIENT_ID"),
        "OIDC_CLIENT_SECRET": settings.get_or_env("OIDC_CLIENT_SECRET"),
        "KEYCLOAK_ADMIN_URL": settings.get_or_env("KEYCLOAK_ADMIN_URL", "http://keycloak:8080"),
        "KEYCLOAK_ADMIN_USER": settings.get_or_env("KEYCLOAK_ADMIN_USER", "admin"),
        "KEYCLOAK_ADMIN_PASSWORD": settings.get_or_env("KEYCLOAK_ADMIN_PASSWORD", "admin"),
    }


async def _connector_check(values: dict[str, str]) -> dict[str, bool]:
    return await kc_admin.check_connector(
        values["OIDC_ISSUER"],
        values["KEYCLOAK_ADMIN_URL"],
        values["KEYCLOAK_ADMIN_USER"],
        values["KEYCLOAK_ADMIN_PASSWORD"],
    )


@app.get("/admin/auth/status", response_class=HTMLResponse)
async def admin_auth_status(request: Request) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    status = await _connector_check(_connector_values())
    oidc_class = "status-found" if status["oidc"] else "status-error"
    admin_class = "status-found" if status["admin"] else "status-error"
    oidc_label = "ok" if status["oidc"] else "unreachable"
    admin_label = "ok" if status["admin"] else "unreachable"
    return HTMLResponse(
        '<div class="status-grid">'
        f'<div><span class="badge {oidc_class}">OIDC discovery {oidc_label}</span></div>'
        f'<div><span class="badge {admin_class}">admin token {admin_label}</span></div>'
        "</div>"
    )


@app.post("/admin/auth")
async def admin_auth(
    request: Request,
    issuer: str = Form(...),
    realm: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(""),
    admin_url: str = Form(...),
    admin_user: str = Form(...),
    admin_password: str = Form(""),
    csrf: str = Form(""),
):
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not _csrf_ok(request, csrf):
        return _csrf_reject()
    current = _connector_values()
    candidate = {
        "OIDC_ISSUER": issuer.strip().rstrip("/"),
        "KEYCLOAK_REALM": realm.strip(),
        "OIDC_CLIENT_ID": client_id.strip(),
        "OIDC_CLIENT_SECRET": client_secret if client_secret else current["OIDC_CLIENT_SECRET"],
        "KEYCLOAK_ADMIN_URL": admin_url.strip().rstrip("/"),
        "KEYCLOAK_ADMIN_USER": admin_user.strip(),
        "KEYCLOAK_ADMIN_PASSWORD": (
            admin_password if admin_password else current["KEYCLOAK_ADMIN_PASSWORD"]
        ),
    }
    if not all(candidate[k] for k in _CONNECTOR_KEYS):
        return _admin_outcome_page(400, "auth provider config incomplete", "status-error")
    status = await _connector_check(candidate)
    if not status["oidc"] or not status["admin"]:
        return _admin_outcome_page(400, "auth provider test failed", "status-error")
    for key in _CONNECTOR_KEYS:
        settings.put(key, candidate[key])
    logger.info(
        "groot %s updated Keycloak auth provider issuer=%s realm=%s client_id=%s "
        "admin_url=%s admin_user=%s",
        principal.viewer,
        candidate["OIDC_ISSUER"],
        candidate["KEYCLOAK_REALM"],
        candidate["OIDC_CLIENT_ID"],
        candidate["KEYCLOAK_ADMIN_URL"],
        candidate["KEYCLOAK_ADMIN_USER"],
    )
    return RedirectResponse("/admin/auth", status_code=303)


@app.get("/admin", response_class=HTMLResponse)
async def admin(request: Request) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    kernel_user_count = None
    assertion = _admin_assertion(principal)
    if assertion:
        try:
            resp = await core_client.list_users(assertion)
            if resp.status == core_pb2.CALL_OK:
                kernel_user_count = len(resp.users)
        except Exception:
            logger.exception("ListUsers failed for admin hub")
    return templates.TemplateResponse(
        request,
        "admin/index.html",
        {
            **_admin_template_context(request, principal, "hub"),
            "groups": auth.known_groups(),
            "kernel_user_count": kernel_user_count,
        },
    )


def _admin_forbidden() -> HTMLResponse:
    return HTMLResponse(
        '<main class="shell"><article class="card">'
        '<span class="badge status-error">forbidden</span></article></main>',
        status_code=403,
    )


def _admin_template_context(request: Request, principal: auth.Principal, section: str) -> dict:
    return {
        "authed": True,
        "principal": principal.to_session(),
        "admin_section": section,
        "csrf_token": _csrf_token(request),
    }


_ROSTER_PAGE = 50


async def _load_roster(assertion: str, q: str, offset: int) -> tuple[list | None, int]:
    """(users, total). users is None ⇒ kernel identity unresolved, roster
    unavailable, or a non-OK status — the template renders that honestly. `total`
    is the pre-page match count (kernel-side) for the pager."""
    if not assertion:
        return None, 0
    try:
        resp = await core_client.list_users(
            assertion, query=q, offset=max(offset, 0), limit=_ROSTER_PAGE
        )
    except Exception:
        logger.exception("ListUsers failed")
        return None, 0
    if resp.status != core_pb2.CALL_OK:
        return None, 0
    return list(resp.users), resp.total


def _roster_context(
    request: Request,
    principal: auth.Principal,
    kernel_users: list | None,
    total: int,
    q: str,
    offset: int,
) -> dict:
    return {
        **_admin_template_context(request, principal, "users"),
        "kernel_users": kernel_users,
        "total": total,
        "q": q,
        "offset": max(offset, 0),
        "limit": _ROSTER_PAGE,
        "groups": auth.known_groups(),
    }


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, q: str = "", offset: int = 0) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    assertion = _admin_assertion(principal)
    kernel_users, total = await _load_roster(assertion, q, offset)
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {
            **_roster_context(request, principal, kernel_users, total, q, offset),
            "local_users": localusers.list_users(),
        },
    )


@app.get("/admin/users/roster", response_class=HTMLResponse)
async def admin_users_roster(request: Request, q: str = "", offset: int = 0) -> HTMLResponse:
    """HTMX partial: the roster table + pager for a server-side search/paginate
    (ListUsers query+offset+total) — no client-side ≤500 ceiling. Declared BEFORE
    the `{user_id}` route so "roster" is not mistaken for a uuid."""
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    assertion = _admin_assertion(principal)
    kernel_users, total = await _load_roster(assertion, q, offset)
    return templates.TemplateResponse(
        request,
        "admin/_users_roster.html",
        _roster_context(request, principal, kernel_users, total, q, offset),
    )


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(request: Request, user_id: str) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    assertion = _admin_assertion(principal)
    user = None
    detail_error = None
    status_code = 200
    if not assertion:
        detail_error, status_code = "kernel identity not resolved", 403
    else:
        try:
            resp = await core_client.get_user(assertion, user_id)
        except Exception:
            logger.exception("GetUser failed for admin user detail")
            resp = None
        if resp is None:
            detail_error, status_code = "kernel unavailable", 503
        elif resp.status == core_pb2.CALL_OK:
            user = resp.user
        elif resp.status == core_pb2.CALL_NOT_FOUND:
            detail_error, status_code = "user not found", 404
        else:
            label, _css = _CALL_STATUS_LABEL.get(resp.status, ("error", "status-error"))
            detail_error = label
            status_code = 403 if resp.status == core_pb2.CALL_NOT_AUTHORIZED else 400
    return templates.TemplateResponse(
        request,
        "admin/user_detail.html",
        {
            **_admin_template_context(request, principal, "users"),
            "user": user,
            "groups": auth.known_groups(),
            "detail_error": detail_error,
        },
        status_code=status_code,
    )


@app.get("/admin/auth", response_class=HTMLResponse)
async def admin_auth_page(request: Request) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    try:
        users = await kc_admin.list_users()
    except Exception:
        logger.exception("Keycloak list_users failed")
        users = None  # template shows an honest "Keycloak unavailable"
    assertion = _admin_assertion(principal)
    sso_map, our_groups = await _load_sso_map(assertion)
    return templates.TemplateResponse(
        request,
        "admin/auth.html",
        {
            **_admin_template_context(request, principal, "auth"),
            "connector": _connector_view(),
            "users": users,
            "sso_map": sso_map,
            "our_groups": our_groups,
            "sso_provider": auth.sso_provider(),
            "groups_claim": auth.groups_claim(),
            "roles_claim": auth.roles_claim(),
        },
    )


async def _load_sso_map(assertion: str) -> tuple[list | None, list[str]]:
    """(mappings, our_group_ids). mappings is None ⇒ the SSO-map RPC isn't
    available yet (kernel pre-BE-1 ⇒ gRPC UNIMPLEMENTED) or a non-OK status — the
    template shows that honestly. our_group_ids feeds the "map to" dropdown."""
    if not assertion:
        return None, []
    mappings: list | None = None
    our_groups: list[str] = []
    try:
        resp = await core_client.list_sso_map(assertion)
        mappings = list(resp.mappings) if resp.status == core_pb2.CALL_OK else None
    except Exception:
        logger.exception("ListSsoMap failed (kernel may predate BE-1)")
        mappings = None
    try:
        gresp = await core_client.list_groups(assertion)
        if gresp.status == core_pb2.CALL_OK:
            our_groups = [g.id for g in gresp.groups]
    except Exception:
        logger.exception("ListGroups failed for SSO-map group picker")
    return mappings, our_groups


@app.post("/admin/auth/claims", response_class=HTMLResponse)
async def admin_auth_claims(
    request: Request,
    groups_claim: str = Form(""),
    roles_claim: str = Form(""),
    csrf: str = Form(""),
):
    """Persist WHICH id-token claim carries groups / roles (channel runtime config,
    not kernel state — it governs how the channel reads the token before it derives
    scope). Empty ⇒ fall back to the built-in default for that claim."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not _csrf_ok(request, csrf):
        return _csrf_reject()
    settings.put("OIDC_GROUPS_CLAIM", groups_claim.strip() or auth.DEFAULT_GROUPS_CLAIM)
    settings.put("OIDC_ROLES_CLAIM", roles_claim.strip() or auth.DEFAULT_ROLES_CLAIM)
    return RedirectResponse("/admin/auth", status_code=303)


@app.post("/admin/auth/sso-map", response_class=HTMLResponse)
async def admin_auth_sso_map(
    request: Request,
    op: str = Form(...),
    incoming_group: str = Form(""),
    our_group_id: str = Form(""),
    csrf: str = Form(""),
):
    """CRUD one incoming-SSO-group → our-group mapping (ADR-18 ps-4) over the kernel
    (ManageSsoMap, `manage_access`-gated + audited). Default-deny is unchanged — an
    unmapped incoming group still grants nothing. Provider is the channel's SSO
    provider key (must match how the kernel provisions SSO subjects)."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not _csrf_ok(request, csrf):
        return _csrf_reject()
    op_map = {"put": core_pb2.SSO_MAP_PUT, "delete": core_pb2.SSO_MAP_DELETE}
    if op not in op_map:
        return _admin_outcome_page(400, "bad request", "status-error")
    assertion = _admin_assertion(principal)
    try:
        resp = await core_client.manage_sso_map(
            assertion,
            op_map[op],
            provider=auth.sso_provider(),
            incoming_group=incoming_group.strip(),
            our_group_id=our_group_id.strip(),
        )
    except Exception:
        logger.exception("ManageSsoMap failed (kernel may predate BE-1)")
        return _admin_outcome_page(502, "kernel unreachable or SSO-map RPC not deployed yet",
                                   "status-error")
    if resp.status != core_pb2.CALL_OK:
        label, css_class = _CALL_STATUS_LABEL.get(resp.status, ("error", "status-error"))
        return _admin_outcome_page(409, label, css_class)
    return RedirectResponse("/admin/auth", status_code=303)


@app.get("/admin/connector")
async def admin_connector_legacy(request: Request) -> Response:
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    return RedirectResponse("/admin/auth", status_code=303)


@app.get("/admin/connectors", response_class=HTMLResponse)
async def admin_connectors(request: Request) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    return templates.TemplateResponse(
        request,
        "admin/connectors.html",
        _admin_template_context(request, principal, "connectors"),
    )


@app.get("/admin/tools", response_class=HTMLResponse)
async def admin_tools(request: Request) -> HTMLResponse:
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    return templates.TemplateResponse(
        request,
        "admin/tools.html",
        _admin_template_context(request, principal, "tools"),
    )


@app.get("/admin/groups", response_class=HTMLResponse)
async def admin_groups(request: Request) -> HTMLResponse:
    """Groups list (ListGroups) + per-row lifecycle via ManageGroup: name,
    members, granted scopes/roles. The scope-picker offers known `src:*`/`public`
    (never `private`) plus any scope a group already holds — no invented sources."""
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    assertion = _admin_assertion(principal)
    groups = None
    if assertion:
        try:
            resp = await core_client.list_groups(assertion)
            groups = list(resp.groups) if resp.status == core_pb2.CALL_OK else None
        except Exception:
            logger.exception("ListGroups failed")
            groups = None
    scope_options = list(auth.known_source_scopes())
    for g in groups or []:
        for s in g.granted_scopes:
            if s != "private" and s not in scope_options:
                scope_options.append(s)
    return templates.TemplateResponse(
        request,
        "admin/groups.html",
        {
            **_admin_template_context(request, principal, "groups"),
            "kernel_groups": groups,
            "scope_options": scope_options,
            "baseline_group": auth.baseline_group(),
            "assignable_roles": ["admin", "superadmin"],
        },
    )


@app.get("/admin/roles", response_class=HTMLResponse)
async def admin_roles(request: Request) -> HTMLResponse:
    """Roles list (ListRoles), READ-ONLY: the fixed user/admin/superadmin set with
    derived capabilities + holder counts (roles are administration-only, per the
    decided model — never grown with connectors)."""
    principal = _require_groot(request)
    if principal is None:
        return _admin_forbidden()
    assertion = _admin_assertion(principal)
    roles = None
    if assertion:
        try:
            resp = await core_client.list_roles(assertion)
            roles = list(resp.roles) if resp.status == core_pb2.CALL_OK else None
        except Exception:
            logger.exception("ListRoles failed")
            roles = None
    return templates.TemplateResponse(
        request,
        "admin/roles.html",
        {
            **_admin_template_context(request, principal, "roles"),
            "kernel_roles": roles,
        },
    )


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
        '<p><a class="navlink" href="/admin/users">back to admin</a></p></article></main>',
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
    csrf: str = Form(""),
):
    """ManageUser (invite/deactivate/delete). INVITE also provisions a channel-local
    credential in the SAME action, so the invited user can sign in immediately
    (`Swarm.Identity.invite_user` alone leaves `status='invited'`, and no gRPC path
    yet promotes it to 'active' — `board/todo/jit-provision-rpc` — so the FIRST
    local login must already resolve; that gap is tracked, not silently papered over)."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not _csrf_ok(request, csrf):
        return _csrf_reject()
    assertion = _admin_assertion(principal)
    op_map = {
        "invite": core_pb2.INVITE,
        "deactivate": core_pb2.DEACTIVATE,
        "delete": core_pb2.DELETE,
    }
    if op not in op_map:
        return _admin_outcome_page(400, "bad request", "status-error")
    grp = group.strip()
    if op == "invite" and grp and grp not in auth.known_groups():
        logger.warning("groot %s tried to assign unknown group=%s", principal.viewer, grp)
        return _admin_outcome_page(400, "unknown group", "status-error")
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
            scopes = [auth.scopes_for([grp])[-1]] if grp else []
            localusers.create(
                login.strip(), password, scopes, is_groot=False, created_by=principal.viewer
            )
        except ValueError:
            logger.warning("kernel-invited login=%s already has a local credential", login)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/kernel/access")
async def admin_kernel_access(
    request: Request,
    op: str = Form(...),
    target_user_id: str = Form(""),
    role: str = Form(""),
    group_id: str = Form(""),
    scopes: str = Form(""),
    csrf: str = Form(""),
):
    """ManageAccess (grant/revoke role or group; set a group's scopes) — superadmin
    for role ops, `manage_access` cap for group ops (kernel-enforced, not here)."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not _csrf_ok(request, csrf):
        return _csrf_reject()
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
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/kernel/group", response_class=HTMLResponse)
async def admin_kernel_group(
    request: Request,
    op: str = Form(...),
    group_id: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    role: str = Form(""),
    confirm: str = Form(""),
    csrf: str = Form(""),
):
    """ManageGroup — first-class group lifecycle (ADR-18): create/rename/delete +
    set/clear role + set-scopes. `manage_access` for lifecycle/scopes, superadmin
    for role ops (kernel-enforced, not here). `scopes` arrives as repeated checkbox
    values (read from the raw form); `private` is dropped defensively (the kernel
    hard-denies it too)."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not _csrf_ok(request, csrf):
        return _csrf_reject()
    op_map = {
        "create": core_pb2.GROUP_CREATE,
        "rename": core_pb2.GROUP_RENAME,
        "delete": core_pb2.GROUP_DELETE,
        "set_role": core_pb2.GROUP_SET_ROLE,
        "clear_role": core_pb2.GROUP_CLEAR_ROLE,
        "set_scopes": core_pb2.GROUP_SET_SCOPES,
    }
    if op not in op_map:
        return _admin_outcome_page(400, "bad request", "status-error")
    assertion = _admin_assertion(principal)
    form_data = await request.form()
    scope_list = [
        s.strip() for s in form_data.getlist("scopes") if s.strip() and s.strip() != "private"
    ]
    try:
        resp = await core_client.manage_group(
            assertion,
            op_map[op],
            group_id=group_id.strip(),
            name=name.strip(),
            description=description.strip(),
            role=role.strip(),
            scopes=scope_list,
            confirm=bool(confirm),
        )
    except Exception:
        logger.exception("ManageGroup failed")
        return _admin_outcome_page(502, "kernel unreachable", "status-error")
    if resp.status != core_pb2.CALL_OK:
        label, css_class = _CALL_STATUS_LABEL.get(resp.status, ("error", "status-error"))
        return _admin_outcome_page(409, label, css_class)
    return RedirectResponse("/admin/groups", status_code=303)


@app.post("/admin/kernel/read-conversation", response_class=HTMLResponse)
async def admin_kernel_read_conversation(
    request: Request,
    conversation_id: str = Form(...),
    reason: str = Form(...),
    csrf: str = Form(""),
) -> HTMLResponse:
    """Break-glass (AdminReadConversation, D6): superadmin + `read_any_conversation`
    only; the kernel audits BEFORE returning (Swarm.Audit) — reason is required at
    the wire boundary (an empty reason is CALL_BAD_REQUEST, not an unlogged read).
    Renders the audited fact visibly: the channel itself sent `reason` on this
    exact request, and the kernel's contract guarantees it logged it first."""
    principal = _require_groot(request)
    if principal is None:
        return HTMLResponse('<span class="badge status-error">forbidden</span>', status_code=403)
    if not _csrf_ok(request, csrf):
        return _csrf_reject()
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


@app.post("/ask/start", response_class=HTMLResponse)
async def ask_start(
    request: Request, q: str = Form(...), thread_id: str = Form("")
) -> HTMLResponse:
    """Phase 1 of an ask (thinking-block, cheap step): return the PENDING fragment
    instantly — the question + a live honest elapsed counter — whose embedded
    auto-firing form then runs the real (slow) /ask and replaces it with the final
    post/reply. No cognition here: just the immediate claude.ai-style feedback."""
    if not q.strip():
        return HTMLResponse("")

    if auth.oidc_enabled():
        principal = _current_principal(request)
        if principal is None:
            return HTMLResponse(
                '<article class="card"><span class="badge status-warn">sign in</span>'
                '<p class="muted">Your session ended. '
                '<a href="/login">Log in</a> to ask.</p></article>'
            )
        viewer = principal.viewer
    else:
        viewer = _viewer()

    asked_at = datetime.fromtimestamp(time.time()).strftime("%Y-%m-%d %H:%M:%S")
    qs = q.strip()
    if thread_id.strip():
        # Validate the thread NOW (viewer-scoped) — instant honest feedback beats
        # a pending block that dies a minute later on an unresolvable thread.
        try:
            root = convlog.get(viewer, int(thread_id))
        except Exception:
            root = None
        if root is None:
            return HTMLResponse('<p class="muted">This post is gone — start a new one.</p>')
        return templates.TemplateResponse(
            request,
            "_reply_pending.html",
            {"q": qs, "thread_id": root["id"], "asked_at": asked_at},
        )

    title, rest = _split_question(qs)
    # Mint the post's permalink slug NOW and push its URL immediately (operator:
    # posting must land you on the post's page) — the pending form carries the slug
    # through to /ask, which persists it, so the pushed URL and the row agree.
    slug = convlog.new_slug()
    return templates.TemplateResponse(
        request,
        "_post_pending.html",
        {"q": qs, "q_title": title, "q_rest": rest, "asked_at": asked_at, "slug": slug},
        headers={"HX-Push-Url": f"/p/{slug}"},
    )


@app.post("/ask", response_class=HTMLResponse)
async def ask(
    request: Request, q: str = Form(...), thread_id: str = Form(""), slug: str = Form("")
) -> HTMLResponse:
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

    # Post-objects rework: a bare ask = a NEW post object (fresh topic — no carried
    # context); an ask with `thread_id` = a REPLY under that post, continuing ITS
    # conversation (per-thread memory, epic 2). The root lookup is viewer-scoped, so
    # a foreign/bogus thread_id resolves to nothing — an honest error, never a leak.
    root: dict | None = None
    if thread_id.strip():
        try:
            root = convlog.get(viewer, int(thread_id))
        except Exception:  # malformed id and a DB failure alike — honest error below
            root = None
        if root is None:
            return HTMLResponse('<p class="muted">This post is gone — start a new one.</p>')

    qs = q.strip()
    asked_at = time.time()
    started = time.monotonic()
    if root is not None:
        kernel_conv = root.get("kernel_conv_id", "")
        if not kernel_conv and assertion:
            # The thread has no kernel conversation yet (its root predates threading
            # or its dual-write failed) — backfill the WHOLE existing thread into a
            # fresh kernel conversation NOW, so even this FIRST reply's Ask gets real
            # history to fold in, not just citation keys. Best-effort: on any failure
            # the reply proceeds exactly as before (active_keys only).
            kernel_conv = await _backfill_thread(assertion, viewer, root)
        try:
            # Thread context = the root's keys UNION the latest turn's keys — the
            # thread's subject survives even when the last reply's citations drifted.
            last = convlog.last_turn(viewer, root["id"])
            active_keys = list(dict.fromkeys(_active_keys_of(root) + _active_keys_of(last)))
        except Exception:
            logger.exception("convlog read failed while computing active_keys")
            active_keys = []
    else:
        kernel_conv, active_keys = "", []

    answer_text, tier, status_str, conf, cites, ask_ref = "", "error", "error", 0.0, [], ""
    try:
        resp = await core_client.ask(
            q,
            scopes=scopes,
            viewer=assertion or viewer,
            active_keys=active_keys,
            conversation_id=kernel_conv,
        )
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
    # The returned row id is the new post's thread handle (its reply form needs it).
    row_id: int | None = None
    try:
        row_id = convlog.log_turn(
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
            thread_id=root["id"] if root else None,
            kernel_conv_id=kernel_conv,
            # A new post keeps the slug /ask/start pre-minted (the pushed URL must
            # resolve); a reply mints its own inside log_turn.
            slug=slug.strip() if not root else "",
        )
    except Exception:
        logger.exception("convlog write failed")

    # Dual-write to the kernel-owned, owner-enforced conversation store (ADR-16
    # D9/step 6b) — best-effort, must never break /ask. The LOCAL convlog above
    # remains the primary read path (it carries the answer trace — tier/confidence/
    # citations — the kernel's Conversation model doesn't). One kernel conversation
    # per POST (post-objects rework): created on the root ask, continued by replies.
    # Only possible once a signed identity exists (assertion non-empty).
    if assertion:
        try:
            title, _ = _split_question(root["question"] if root else qs)
            user_msg = await core_client.log_conversation(
                assertion, conversation_id=kernel_conv, title=title, role="user", body=qs
            )
            if user_msg.status == core_pb2.CALL_OK:
                if not kernel_conv:
                    # A conversation was just created for this thread — remember it on
                    # the ROOT row so the next reply continues the same memory. (For a
                    # reply whose root predates signing, this backfills the thread.)
                    anchor = root["id"] if root else row_id
                    if anchor:
                        convlog.set_kernel_conv(viewer, anchor, user_msg.conversation_id)
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
        "id": row_id,  # a reply is a first-class post — its anchor/graph identity
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
    if root is not None:
        # A reply, appended inside its post object's thread.
        return templates.TemplateResponse(request, "_post_reply.html", {"post": _post_view(turn)})
    # A new post object, prepended to the feed.
    view = _post_view(turn)
    view["thread_id"] = row_id
    view["replies"] = []
    return templates.TemplateResponse(request, "_post_object.html", {"post": view})


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"
