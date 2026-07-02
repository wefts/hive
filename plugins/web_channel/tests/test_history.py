"""Kernel-owned conversation history (ADR-16 D9, step 6b.3): /ask dual-writes to
`LogConversation` when a signed identity exists (the local convlog stays the
primary read path and the record when signing isn't configured yet); /history and
/history/{id} render List/Get for real. `ask_ref` is verified to survive the move
(MessageView carries it — confirmed against the kernel proto/server, not assumed)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_channel import auth, core_client, localusers
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def _signed_client(monkeypatch, username: str) -> TestClient:
    """A fresh TestClient logged in as a LOCAL user with a working signed identity
    (SWARM_ACTOR_SECRET configured, ResolveActor mocked OK)."""
    monkeypatch.setenv("SWARM_ACTOR_SECRET", "a" * 32)
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)

    async def fake_resolve(assertion):
        return core_pb2.ResolveActorResponse(status=core_pb2.CALL_OK, uuid=f"u-{username}", caps=[])

    monkeypatch.setattr(core_client, "resolve_actor", fake_resolve)
    localusers.create(username, "pw", [], created_by="t")
    c = TestClient(web.app)
    c.post("/login/local", data={"identifier": username, "password": "pw"})
    return c


async def _fake_ask(query, scopes, viewer):
    return core_pb2.AskResponse(
        answer="the answer", status=core_pb2.FOUND, tier="t", confidence=0.8
    )


def test_ask_dual_writes_to_kernel_when_signed(monkeypatch) -> None:
    c = _signed_client(monkeypatch, "penny")
    monkeypatch.setattr(core_client, "ask", _fake_ask)
    calls: list[dict] = []

    async def fake_log(assertion, conversation_id="", title="", role="", body="", ask_ref=""):
        is_first_call = len(calls) == 0
        calls.append(
            {
                "assertion": assertion,
                "conversation_id": conversation_id,
                "title": title,
                "role": role,
                "body": body,
                "ask_ref": ask_ref,
            }
        )
        if is_first_call:
            return core_pb2.LogConversationResponse(
                status=core_pb2.CALL_OK, conversation_id="conv-1", message_id="m-1"
            )
        return core_pb2.LogConversationResponse(status=core_pb2.CALL_OK, message_id="m-2")

    monkeypatch.setattr(core_client, "log_conversation", fake_log)
    c.post("/ask", data={"q": "what is X?"})

    assert len(calls) == 2
    assert calls[0]["role"] == "user" and calls[0]["body"] == "what is X?"
    assert calls[0]["assertion"].count(".") == 2  # signed, not plaintext
    assert calls[1]["role"] == "assistant" and calls[1]["body"] == "the answer"
    assert calls[1]["conversation_id"] == "conv-1"  # threaded onto the created conversation


def test_ask_dual_write_failure_never_breaks_ask(monkeypatch) -> None:
    c = _signed_client(monkeypatch, "quinn")
    monkeypatch.setattr(core_client, "ask", _fake_ask)

    async def boom(*a, **kw):
        raise RuntimeError("kernel LogConversation down")

    monkeypatch.setattr(core_client, "log_conversation", boom)
    r = c.post("/ask", data={"q": "anything"})
    assert r.status_code == 200 and "the answer" in r.text  # /ask still succeeded


def test_ask_skips_kernel_write_without_signed_identity(monkeypatch) -> None:
    # P0 mode (OIDC off): no principal, no assertion — must not even attempt it.
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    monkeypatch.setattr(core_client, "ask", _fake_ask)

    async def must_not_call(*a, **kw):
        raise AssertionError("LogConversation must not be called without a signed identity")

    monkeypatch.setattr(core_client, "log_conversation", must_not_call)
    r = TestClient(web.app).post("/ask", data={"q": "anything"})
    assert r.status_code == 200


def test_history_not_signed_shows_honest_message(monkeypatch) -> None:
    monkeypatch.delenv("SWARM_ACTOR_SECRET", raising=False)
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    r = client.get("/history")
    assert r.status_code == 200
    assert "not available" in r.text.lower()


def test_history_lists_kernel_conversations(monkeypatch) -> None:
    c = _signed_client(monkeypatch, "riley")

    async def fake_list(assertion):
        return core_pb2.ListConversationsResponse(
            status=core_pb2.CALL_OK,
            conversations=[
                core_pb2.ConversationView(id="conv-9", title="what is Keycloak?", created_at="t0")
            ],
        )

    monkeypatch.setattr(core_client, "list_conversations", fake_list)
    r = c.get("/history")
    assert r.status_code == 200
    assert "what is Keycloak?" in r.text
    assert 'href="/history/conv-9"' in r.text


def test_history_thread_renders_messages_and_ask_ref_link(monkeypatch) -> None:
    c = _signed_client(monkeypatch, "sam")

    async def fake_get(assertion, conversation_id):
        return core_pb2.GetConversationResponse(
            status=core_pb2.CALL_OK,
            conversation=core_pb2.ConversationView(id=conversation_id, title="thread"),
            messages=[
                core_pb2.MessageView(id="m1", role="user", body="q?", created_at="t0"),
                core_pb2.MessageView(
                    id="m2", role="assistant", body="a.", ask_ref="ref-123", created_at="t1"
                ),
            ],
        )

    monkeypatch.setattr(core_client, "get_conversation", fake_get)
    r = c.get("/history/conv-9")
    assert r.status_code == 200
    assert "q?" in r.text and "a." in r.text
    assert 'href="/deliberation/ref-123"' in r.text  # ask_ref survives the move


def test_history_thread_not_found_is_honest(monkeypatch) -> None:
    c = _signed_client(monkeypatch, "tara")

    async def fake_get(assertion, conversation_id):
        return core_pb2.GetConversationResponse(status=core_pb2.CALL_NOT_FOUND)

    monkeypatch.setattr(core_client, "get_conversation", fake_get)
    r = c.get("/history/not-mine")
    assert r.status_code == 200
    assert "not found" in r.text.lower()
