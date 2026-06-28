"""web_channel — FastAPI operator console over the kernel Core API (P0).

GET /        the input box (index)
POST /ask    runs Core.Ask, returns the answer-card HTMX partial
GET /healthz liveness (no kernel call)

Rendering is deterministic (see render.py); Jinja2 autoescape keeps every value
verbatim and HTML-safe (brief A.0.4). The channel holds no cognition.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from grpc import aio

from web_channel import core_client, render
from web_channel._gen import core_pb2

logger = logging.getLogger("web_channel")
_HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

app = FastAPI(title="Swarm web_channel", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")


def _viewer() -> str:
    # Fixed operator identity until P1 auth. Empty ⇒ anonymous (limited).
    return os.environ.get("SWARM_VIEWER", "operator")


def _scopes() -> list[str]:
    # P0 is PRE-AUTH: this web surface has no authenticated identity, so it is
    # HARD-LOCKED to public scope — no env may widen it. Scope/privacy no-leak is
    # the one hard invariant, and an unauthenticated port must never be able to
    # request private data. P1 replaces this with scopes derived from the
    # authenticated user (the kernel remains the sole scope authority either way).
    return ["public"]


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
    return templates.TemplateResponse(request, "index.html", {})


@app.post("/ask", response_class=HTMLResponse)
async def ask(request: Request, q: str = Form(...)) -> HTMLResponse:
    # The HTML `required` is client-only and bypassable; don't spend an Ask on an
    # empty/whitespace query — just clear the answer region.
    if not q.strip():
        return HTMLResponse("")
    try:
        resp = await core_client.ask(q, scopes=_scopes(), viewer=_viewer())
        ctx = _answer_view(resp)
    except aio.AioRpcError as err:
        # Unreachable / DEADLINE_EXCEEDED / etc. — honest error with the gRPC code.
        ctx = _error_view(err.code().name)
    except Exception:
        # Never crash the page or leak internals (brief A.0.3): log server-side,
        # show a generic error card.
        logger.exception("unexpected error handling /ask")
        ctx = _error_view("internal")
    return templates.TemplateResponse(request, "_answer.html", ctx)


@app.get("/healthz", response_class=PlainTextResponse)
async def healthz() -> str:
    return "ok"
