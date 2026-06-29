"""App-level tests: deterministic rendering, honest states, and escaping, driven
through the FastAPI app with a faked Core client (no live kernel). Mirrors the
intent of swarm/cli's test_cli.py, against the P0 acceptance criteria A.0.1-A.0.4.
"""

from __future__ import annotations

import grpc
from fastapi.testclient import TestClient
from grpc import aio

from web_channel import core_client
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _fake_ask(resp: core_pb2.AskResponse, captured: dict | None = None):
    async def ask(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
        if captured is not None:
            captured.update(query=query, scopes=scopes, viewer=viewer)
        return resp

    return ask


def test_index_renders_input_box() -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-post="/ask"' in r.text
    assert 'name="q"' in r.text
    # local-first: assets are vendored, no external network call
    assert "/static/vendor/htmx.min.js" in r.text
    assert "https://" not in r.text.split("<body")[0].replace("initial-scale", "")


def test_a01_found_renders_answer_and_verbatim_citation(monkeypatch) -> None:
    resp = core_pb2.AskResponse(
        answer="Postgres + pgvector.",
        confidence=0.82,
        tier="tier_tools",
        status=core_pb2.FOUND,
        citations=[core_pb2.Citation(source="file", ref="/docs/storage.md", confidence=0.9)],
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "which storage engine?"})
    assert r.status_code == 200
    assert "found" in r.text
    assert "Postgres + pgvector." in r.text
    assert "/docs/storage.md" in r.text  # verbatim
    assert "file" in r.text
    assert "0.82" in r.text  # confidence shown for FOUND


def test_a02_not_found_is_honest_no_fabricated_citation_or_confidence(monkeypatch) -> None:
    # answer prose has NO status words: a match on the label proves it came from
    # the structured field, not the prose (determinism).
    resp = core_pb2.AskResponse(
        answer="(no matches in scope)",
        confidence=0.3,
        tier="tier_tools",
        status=core_pb2.NOT_FOUND,
        citations=[core_pb2.Citation(source="ghost", ref="ghost-ref", confidence=0.9)],
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "missing thing"})
    assert r.status_code == 200
    assert "not found" in r.text
    # no fabricated confidence number for a not_found
    assert "0.30" not in r.text and "confidence=" not in r.text
    # citations are suppressed on a non-found result (no fabricated evidence)
    assert "ghost" not in r.text


def test_a03_kernel_unreachable_renders_honest_error(monkeypatch) -> None:
    async def failing_ask(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
        raise aio.AioRpcError(
            grpc.StatusCode.UNAVAILABLE, aio.Metadata(), aio.Metadata(), details="down"
        )

    monkeypatch.setattr(core_client, "ask", failing_ask)
    r = client.post("/ask", data={"q": "anything"})
    assert r.status_code == 200  # no crash
    assert "could not reach the knowledge base" in r.text.lower()
    assert "UNAVAILABLE" in r.text  # honest gRPC code surfaced
    assert "confidence" not in r.text.lower()  # no fabricated certainty on error


def test_a04_adversarial_citation_ref_renders_verbatim_escaped(monkeypatch) -> None:
    nasty = "a<b&c[d`e"
    resp = core_pb2.AskResponse(
        answer="x<y&z",
        confidence=0.7,
        tier="tier_tools",
        status=core_pb2.FOUND,
        citations=[core_pb2.Citation(source="src", ref=nasty, confidence=0.5)],
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "special"})
    assert r.status_code == 200
    # < and & are HTML-escaped (so they render literally, not as broken markup);
    # [ and ` are not HTML-special and pass through verbatim.
    assert "a&lt;b&amp;c[d`e" in r.text
    assert "x&lt;y&amp;z" in r.text
    # the raw, unescaped sequence must NOT appear (that would be broken/injected markup)
    assert "a<b&c" not in r.text


def test_viewer_passes_through_and_scopes_locked_to_public(monkeypatch) -> None:
    # P0 is pre-auth: viewer is a configurable identity string, but scope is
    # HARD-LOCKED to public — no env may widen it (the one hard privacy invariant).
    captured: dict = {}
    resp = core_pb2.AskResponse(answer="ok", confidence=0.7, tier="t", status=core_pb2.FOUND)
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp, captured))
    monkeypatch.setenv("SWARM_VIEWER", "alice")
    # Even a hostile env trying to widen scope must be ignored.
    monkeypatch.setenv("SWARM_SCOPES", "private,secret,group")
    client.post("/ask", data={"q": "my ticket"})
    assert captured["viewer"] == "alice"
    assert captured["scopes"] == ["public"]  # locked; env cannot widen a pre-auth surface


def test_partial_renders_confidence_and_citations(monkeypatch) -> None:
    resp = core_pb2.AskResponse(
        answer="partial answer",
        confidence=0.55,
        tier="tier_tools",
        status=core_pb2.PARTIAL,
        citations=[core_pb2.Citation(source="file", ref="/p.md", confidence=0.6)],
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "partial?"})
    assert r.status_code == 200
    assert "partial" in r.text
    assert "/p.md" in r.text  # citations shown for PARTIAL
    assert "0.55" in r.text  # confidence shown for PARTIAL


def test_unexpected_exception_renders_generic_error_no_leak(monkeypatch) -> None:
    async def boom(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
        raise ValueError("secret internal detail")

    monkeypatch.setattr(core_client, "ask", boom)
    r = client.post("/ask", data={"q": "anything"})
    assert r.status_code == 200  # no crash (A.0.3)
    assert "something went wrong" in r.text.lower()  # generic message, honest
    # internals must NOT leak into the page
    assert "secret internal detail" not in r.text
    assert "ValueError" not in r.text and "Traceback" not in r.text


def test_empty_query_clears_without_calling_kernel(monkeypatch) -> None:
    called = {"n": 0}

    async def counting_ask(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
        called["n"] += 1
        return core_pb2.AskResponse(status=core_pb2.FOUND)

    monkeypatch.setattr(core_client, "ask", counting_ask)
    r = client.post("/ask", data={"q": "   "})
    assert r.status_code == 200
    assert r.text.strip() == ""  # answer region cleared
    assert called["n"] == 0  # no Ask spent on an empty query
