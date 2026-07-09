"""Conversation-log tests: durable persistence, per-viewer isolation, and that
/ask records a turn + the dashboard shows durable history."""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_channel import auth, convlog, core_client
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def test_log_and_recent_newest_first_per_viewer() -> None:
    convlog.log_turn("alice", ["public", "group"], "q1", "a1", "escalate", "found", 0.9, [])
    convlog.log_turn("alice", ["public", "group"], "q2", "a2", "tier_tools", "found", 0.7, [])
    convlog.log_turn("bob", ["public"], "qb", "ab", "tier0", "found", 0.9, [])

    alice = convlog.recent("alice", 10)
    assert [t["question"] for t in alice] == ["q2", "q1"]  # newest first
    assert convlog.recent("bob", 10)[0]["question"] == "qb"
    assert all(t["question"] != "qb" for t in alice)  # per-viewer isolation


def test_ask_persists_a_turn(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)  # viewer = operator

    async def fake_ask(query, scopes, viewer, **kwargs):
        return core_pb2.AskResponse(
            answer="Postgres.",
            status=core_pb2.FOUND,
            tier="escalate",
            confidence=0.8,
            citations=[core_pb2.Citation(source="file", ref="/x.md", confidence=0.9)],
        )

    monkeypatch.setattr(core_client, "ask", fake_ask)
    client.post("/ask", data={"q": "which db?"})
    turns = convlog.recent("operator", 10)
    assert turns and turns[0]["question"] == "which db?"
    assert turns[0]["tier"] == "escalate" and turns[0]["status"] == "found"
    assert turns[0]["citations"][0]["ref"] == "/x.md"


def test_ask_error_is_logged_as_error(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def boom(query, scopes, viewer):
        raise RuntimeError("kernel down")

    monkeypatch.setattr(core_client, "ask", boom)
    client.post("/ask", data={"q": "anything"})
    assert convlog.recent("operator", 1)[0]["status"] == "error"


def test_answer_card_shows_trace_path(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake_ask(query, scopes, viewer, **kwargs):
        return core_pb2.AskResponse(
            answer="x", status=core_pb2.FOUND, tier="escalate", confidence=0.8
        )

    monkeypatch.setattr(core_client, "ask", fake_ask)
    r = client.post("/ask", data={"q": "q"})
    assert "consilium" in r.text.lower()  # the gate→consilium trace path is shown


def test_conversation_reopen_renders_past_turn(monkeypatch) -> None:
    from web_channel import auth

    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)  # viewer = operator
    convlog.log_turn(
        "operator",
        ["public"],
        "what is X?",
        "X is a thing.",
        "escalate",
        "found",
        0.8,
        [{"source": "file", "ref": "/x.md", "confidence": 0.9}],
    )
    cid = convlog.recent("operator", 1)[0]["id"]
    r = client.get(f"/conversation/{cid}")
    assert r.status_code == 200
    assert "what is X?" in r.text and "X is a thing." in r.text and "/x.md" in r.text


def test_conversation_missing_is_404(monkeypatch) -> None:
    from web_channel import auth

    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    r = client.get("/conversation/99999999")
    assert r.status_code == 404


def test_conversation_is_viewer_scoped(monkeypatch) -> None:
    # A viewer can only reopen their OWN conversations (convlog.get filters by viewer).
    from web_channel import auth

    convlog.log_turn(
        "alice", ["public", "group"], "secret q", "secret a", "escalate", "found", 0.9, []
    )
    cid = convlog.recent("alice", 1)[0]["id"]
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)  # viewer = operator, not alice
    r = client.get(f"/conversation/{cid}")
    assert r.status_code == 404  # operator cannot open alice's conversation


def test_threads_roots_in_recent_replies_under_their_root() -> None:
    # Post-objects rework: a reply (thread_id = root's row id) never shows up as
    # standalone history — it lives under its root; last_turn is the thread's tail.
    root_id = convlog.log_turn("alice", ["public"], "root q", "root a", "t", "found", 0.9, [])
    convlog.log_turn(
        "alice", ["public"], "follow q", "follow a", "t", "found", 0.8, [], thread_id=root_id
    )

    roots = convlog.recent("alice", 10)
    assert [t["question"] for t in roots] == ["root q"]  # the reply is not history

    thread = convlog.replies("alice", root_id)
    assert [t["question"] for t in thread] == ["follow q"]
    tail = convlog.last_turn("alice", root_id)
    assert tail is not None and tail["question"] == "follow q"
    # and per-viewer isolation holds for thread reads too
    assert convlog.replies("bob", root_id) == []
    assert convlog.last_turn("bob", root_id) is None


def test_set_kernel_conv_binds_the_thread_to_its_kernel_conversation() -> None:
    root_id = convlog.log_turn("alice", ["public"], "q", "a", "t", "found", 0.9, [])
    convlog.set_kernel_conv("alice", root_id, "conv-77")
    root = convlog.get("alice", root_id)
    assert root is not None and root["kernel_conv_id"] == "conv-77"
    # viewer-scoped: bob can't rebind alice's thread
    convlog.set_kernel_conv("bob", root_id, "evil")
    root = convlog.get("alice", root_id)
    assert root is not None and root["kernel_conv_id"] == "conv-77"


def test_every_turn_gets_a_slug_and_old_rows_are_backfilled() -> None:
    # Every post (root AND reply — replies are first-class posts) carries a public
    # YouTube-shaped permalink slug; rows from before slugs existed get one at init.
    rid = convlog.log_turn("alice", ["public"], "q", "a", "t", "found", 0.9, [])
    row = convlog.get("alice", rid)
    assert row is not None and len(row["slug"]) >= 8
    assert convlog.get_by_slug("alice", row["slug"]) is not None
    assert convlog.get_by_slug("bob", row["slug"]) is None  # viewer-scoped, no leak

    # simulate a pre-slug row, then re-init → backfilled
    with convlog._conn() as conn:
        conn.execute("UPDATE conversations SET slug = NULL WHERE id = ?", (rid,))
    convlog._initialized = False
    convlog.init()
    refreshed = convlog.get("alice", rid)
    assert refreshed is not None and refreshed["slug"]


def test_post_page_by_slug_and_ask_start_pushes_the_permalink(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    # a fresh ask/start mints the slug and pushes the post's URL immediately
    r = client.post("/ask/start", data={"q": "a permalinked question?"})
    assert r.status_code == 200
    assert r.headers.get("hx-push-url", "").startswith("/p/")
    slug = r.headers["hx-push-url"].removeprefix("/p/")
    assert f'name="slug" value="{slug}"' in r.text  # /ask will persist the SAME slug

    async def fake_ask(query, scopes, viewer, **kwargs):
        return core_pb2.AskResponse(answer="ok", status=core_pb2.FOUND, tier="t", confidence=0.8)

    monkeypatch.setattr(core_client, "ask", fake_ask)
    client.post("/ask", data={"q": "a permalinked question?", "slug": slug})

    # the pushed URL resolves to the post's own PAGE (full layout + the thread)
    page = client.get(f"/p/{slug}")
    assert page.status_code == 200
    assert "a permalinked question?" in page.text and 'class="post-object"' in page.text
    # and the sidebar links are REAL permalinks (open-in-new-tab must work)
    assert f'href="/p/{slug}"' in page.text
    assert 'href="#"' not in page.text
