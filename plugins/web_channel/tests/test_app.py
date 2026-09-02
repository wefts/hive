"""App-level tests: deterministic rendering, honest states, and escaping, driven
through the FastAPI app with a faked Core client (no live kernel). Mirrors the
intent of swarm/cli's test_cli.py, against the P0 acceptance criteria A.0.1-A.0.4.
"""

from __future__ import annotations

import re

import grpc
from fastapi.testclient import TestClient
from grpc import aio

from web_channel import convlog, core_client
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _fake_ask(resp: core_pb2.AskResponse, captured: dict | None = None):
    async def ask(query: str, scopes: list[str], viewer: str, **kwargs) -> core_pb2.AskResponse:
        if captured is not None:
            captured.update(query=query, scopes=scopes, viewer=viewer)
        return resp

    return ask


def test_index_renders_input_box() -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-post="/ask/start"' in r.text  # phase 1: the instant pending post
    assert 'name="q"' in r.text
    # local-first: assets are vendored, no external network call
    assert "/static/vendor/htmx.min.js" in r.text
    assert "https://" not in r.text.split("<body")[0].replace("initial-scale", "")


def test_index_renders_post_feed_compose_on_top_and_sidebar_history(monkeypatch) -> None:
    """Post-objects rework + chat standard: home = sidebar history + compose on top +
    a feed that new asks APPEND to at the bottom (top-down, oldest first — the chat
    standard; newest-first is a history-page concern, out of scope) — never one
    continuous chat ribbon of past history."""
    convlog.log_turn("operator", ["public"], "an old question", "old answer", "t", "found", 0.9, [])
    r = client.get("/")
    assert 'id="feed"' in r.text
    assert 'hx-target="#feed"' in r.text and 'hx-swap="beforeend"' in r.text
    assert 'hx-swap="afterbegin"' not in r.text  # top-down: never newest-first
    assert "Recent conversations" in r.text and "an old question" in r.text  # sidebar history
    assert 'id="thread"' not in r.text  # the chat ribbon must never come back
    assert "old answer" not in r.text  # history is sidebar links, not a wall on home


def test_ask_returns_a_post_object_with_a_visible_reply_box(monkeypatch) -> None:
    """A fresh ask = one discrete post object: the question, the answer under it, and
    an ALWAYS-VISIBLE reply box carrying the post's thread handle — the collapsed
    affordance sent follow-ups to the top compose as context-free NEW posts
    (operator-observed failure, 2026-07-08)."""
    resp = core_pb2.AskResponse(answer="ok", confidence=0.8, tier="t", status=core_pb2.FOUND)
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "a brand new topic?"})
    assert 'class="post-object"' in r.text
    assert 'class="reply-form"' in r.text and 'name="thread_id"' in r.text
    assert "reply-affordance" not in r.text  # no more hidden follow-up
    # the hidden thread_id is the convlog row id of this very post
    root = convlog.recent("operator", 1)[0]
    assert f'value="{root["id"]}"' in r.text


def test_a01_found_renders_answer_and_verbatim_citation(monkeypatch) -> None:
    resp = core_pb2.AskResponse(
        answer="Postgres + pgvector.",
        confidence=0.82,
        tier="tier_tools",
        status=core_pb2.FOUND,
        citations=[core_pb2.Citation(source="file", ref="/docs/storage.md", confidence=0.9)],
        ask_ref="ref-storage",
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "which storage engine?"})
    assert r.status_code == 200
    assert "found" in r.text
    assert "Postgres + pgvector." in r.text
    assert "/docs/storage.md" in r.text  # verbatim
    assert "file" in r.text
    assert "0.82" in r.text  # confidence shown for FOUND
    assert 'hx-post="/rate"' in r.text
    assert 'name="ask_ref" value="ref-storage"' in r.text
    assert 'name="csrf"' in r.text


def test_found_renders_citation_link_when_kernel_supplies_url(monkeypatch) -> None:
    resp = core_pb2.AskResponse(
        answer="Article answer.",
        confidence=0.82,
        tier="escalate",
        status=core_pb2.FOUND,
        citations=[
            core_pb2.Citation(
                source="article",
                ref="Example.test Article",
                confidence=0.9,
                url="https://docs.example.test/pages/123",
            )
        ],
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "where is this written?"})
    assert r.status_code == 200
    assert 'href="https://docs.example.test/pages/123"' in r.text
    assert "Example.test Article" in r.text


def test_rate_answer_round_trips_to_kernel(monkeypatch) -> None:
    captured: dict = {}

    async def fake_rate_answer(ask_ref: str, scopes: list[str], viewer: str, rating: int):
        captured.update(ask_ref=ask_ref, scopes=scopes, viewer=viewer, rating=rating)
        return core_pb2.RateAnswerResponse(status=core_pb2.CALL_OK, ask_ref=ask_ref, rating=rating)

    monkeypatch.setattr(core_client, "rate_answer", fake_rate_answer)
    ask_resp = core_pb2.AskResponse(
        answer="Postgres + pgvector.",
        confidence=0.82,
        tier="tier_tools",
        status=core_pb2.FOUND,
        ask_ref="ref-storage",
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(ask_resp))
    rendered = client.post("/ask", data={"q": "which storage engine?"})
    m = re.search(r'name="csrf" value="([^"]+)"', rendered.text)
    assert m

    r = client.post("/rate", data={"ask_ref": "ref-storage", "rating": "wrong", "csrf": m.group(1)})

    assert r.status_code == 200
    assert "Rating saved" in r.text
    assert captured == {
        "ask_ref": "ref-storage",
        "scopes": ["public"],
        "viewer": "operator",
        "rating": core_pb2.WRONG,
    }


def test_rate_answer_rejects_missing_csrf(monkeypatch) -> None:
    called = False

    async def fake_rate_answer(ask_ref: str, scopes: list[str], viewer: str, rating: int):
        nonlocal called
        called = True

    monkeypatch.setattr(core_client, "rate_answer", fake_rate_answer)

    r = client.post("/rate", data={"ask_ref": "ref-storage", "rating": "wrong"})

    assert r.status_code == 403
    assert not called


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
    async def failing_ask(
        query: str, scopes: list[str], viewer: str, **kwargs
    ) -> core_pb2.AskResponse:
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


def test_adversarial_citation_url_is_escaped(monkeypatch) -> None:
    resp = core_pb2.AskResponse(
        answer="x",
        confidence=0.7,
        tier="tier_tools",
        status=core_pb2.FOUND,
        citations=[
            core_pb2.Citation(
                source="src",
                ref="safe ref",
                confidence=0.5,
                url='https://docs.example.test/" onclick="alert(1)',
            )
        ],
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "special"})
    assert r.status_code == 200
    assert 'onclick="alert(1)' not in r.text
    assert "https://docs.example.test/&#34; onclick=&#34;alert(1)" in r.text


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

    async def counting_ask(
        query: str, scopes: list[str], viewer: str, **kwargs
    ) -> core_pb2.AskResponse:
        called["n"] += 1
        return core_pb2.AskResponse(status=core_pb2.FOUND)

    monkeypatch.setattr(core_client, "ask", counting_ask)
    r = client.post("/ask", data={"q": "   "})
    assert r.status_code == 200
    assert r.text.strip() == ""  # answer region cleared
    assert called["n"] == 0  # no Ask spent on an empty query


def test_markdown_title_rules(monkeypatch) -> None:
    # 1.3 — an explicit `# Heading` names the topic (post header AND sidebar).
    assert web._split_question("# Deploy plan\nHere is the body.") == (
        "Deploy plan",
        "Here is the body.",
    )
    # 1.1 — a short post is all title (microblog).
    assert web._split_question("who is erker") == ("who is erker", "")
    # 1.2 — long post: first sentence (or first line) is the title.
    long_q = "What is the plan for LDAP?\nWe need failover and a second replica soon."
    t, rest = web._split_question(long_q)
    assert t == "What is the plan for LDAP?" and rest.startswith("We need failover")
    # an overlong first sentence is display-truncated; the body keeps the FULL text
    huge = "word " * 40
    t, rest = web._split_question(huge)
    assert t.endswith("…") and len(t) <= web._TITLE_MAX and rest == huge.strip()


def test_answer_renders_markdown_lists_and_autolinks(monkeypatch) -> None:
    resp = core_pb2.AskResponse(
        answer="Links:\n* https://tmate.io/ — terminal sharing\n* https://explainshell.com/",
        confidence=0.9,
        tier="escalate",
        status=core_pb2.FOUND,
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "give me the links"})
    assert "<ul>" in r.text and "<li>" in r.text  # a real list, not raw asterisks
    assert '<a href="https://tmate.io/"' in r.text  # bare URL autolinked


def test_markdown_never_passes_raw_html_through(monkeypatch) -> None:
    # html=False: script tags in a question OR an answer render ESCAPED, never live.
    resp = core_pb2.AskResponse(
        answer='<script>alert("a")</script>', confidence=0.9, tier="t", status=core_pb2.FOUND
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": 'hi <script>alert("q")</script>\nmore text'})
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_chat_keys_guard_unclosed_code_blocks(monkeypatch) -> None:
    # Mattermost behavior (operator): Enter sends, Shift+Enter is a newline, and an
    # UNCLOSED ``` code block holds the send (Enter = newline until the fence closes)
    # so a half-written block can't be posted by accident. Client-side — pin the
    # handler's presence on both the compose and the reply box.
    resp = core_pb2.AskResponse(answer="ok", confidence=0.8, tier="t", status=core_pb2.FOUND)
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    home = client.get("/")
    assert home.text.count("match(/```/g)") == 1  # the compose
    r = client.post("/ask", data={"q": "any post"})
    assert r.text.count("match(/```/g)") == 1  # the reply box in the post object


def test_code_blocks_are_syntax_highlighted_server_side(monkeypatch) -> None:
    # Local-first highlighting (operator): pygments spans server-side — no CDN, no
    # client JS. A labeled fence uses its language; an UNLABELED one is guessed.
    resp = core_pb2.AskResponse(
        answer='```yaml\nbuild:\n  image: "moby/buildkit"\n```',
        confidence=0.9,
        tier="t",
        status=core_pb2.FOUND,
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp))
    r = client.post("/ask", data={"q": "show me the job"})
    assert '<span class="' in r.text.split("<pre>")[1]  # real token spans in the block
    # code content stays escaped even through the highlighter
    resp2 = core_pb2.AskResponse(
        answer='```html\n<script>alert("x")</script>\n```',
        confidence=0.9,
        tier="t",
        status=core_pb2.FOUND,
    )
    monkeypatch.setattr(core_client, "ask", _fake_ask(resp2))
    r2 = client.post("/ask", data={"q": "and html?"})
    # pygments token spans SPLIT the escaped text (&lt;</span>...script), so assert
    # the raw tag is absent rather than looking for a contiguous escaped string.
    assert "<script>" not in r2.text
    assert "&lt;" in r2.text.split("<pre>")[1]  # escaped inside the highlighted block
