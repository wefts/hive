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

    async def fake_ask(query, scopes, viewer):
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

    async def fake_ask(query, scopes, viewer):
        return core_pb2.AskResponse(
            answer="x", status=core_pb2.FOUND, tier="escalate", confidence=0.8
        )

    monkeypatch.setattr(core_client, "ask", fake_ask)
    r = client.post("/ask", data={"q": "q"})
    assert "consilium" in r.text.lower()  # the gate→consilium trace path is shown
