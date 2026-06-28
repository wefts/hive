"""P2 dashboard tests: cold-open dashboard, the KbStatus tile (real + honest
unavailable), the ⌘K search palette (scope-respecting), and session ask-history.
Driven through the app with the Core client faked (no live kernel)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_channel import auth, core_client
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


def test_index_is_dashboard_when_oidc_off(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    r = client.get("/")
    assert r.status_code == 200
    assert 'hx-get="/tile/status"' in r.text  # the async state-of-memory tile
    assert 'hx-post="/ask"' in r.text  # ask box is on the dashboard
    assert "/search" in r.text  # ⌘K palette wired


def test_status_tile_renders_real_state(monkeypatch) -> None:
    async def fake_status() -> core_pb2.StatusResponse:
        return core_pb2.StatusResponse(
            nodes=2345,
            edges=2648,
            last_activity="2026-06-23T20:35:43Z",
            inventory=[core_pb2.TypeCount(type="article", count=2345)],
            namespaces=[
                core_pb2.NamespaceStamp(
                    namespace="bge-m3", model="bge-m3", dim=1024, status="pending"
                )
            ],
            capabilities=["consilium:4-model-panel"],
        )

    monkeypatch.setattr(core_client, "kb_status", fake_status)
    r = client.get("/tile/status")
    assert r.status_code == 200
    assert "2345" in r.text and "article" in r.text and "bge-m3" in r.text


def test_status_tile_unavailable_on_error(monkeypatch) -> None:
    async def boom() -> core_pb2.StatusResponse:
        raise RuntimeError("kernel down")

    monkeypatch.setattr(core_client, "kb_status", boom)
    r = client.get("/tile/status")
    assert r.status_code == 200  # no crash
    assert "unavailable" in r.text.lower()


def test_search_renders_hits(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake_search(
        query: str, scopes: list[str], limit: int = 10
    ) -> core_pb2.SearchResponse:
        return core_pb2.SearchResponse(
            hits=[core_pb2.SearchHit(id=1, type="article", key="AllMusic", score=0.9)]
        )

    monkeypatch.setattr(core_client, "kb_search", fake_search)
    r = client.get("/search", params={"q": "music"})
    assert r.status_code == 200 and "AllMusic" in r.text


def test_search_empty_query_clears(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    r = client.get("/search", params={"q": "   "})
    assert r.status_code == 200 and r.text.strip() == ""


def test_search_scopes_locked_when_oidc_off(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    captured: dict = {}

    async def fake_search(
        query: str, scopes: list[str], limit: int = 10
    ) -> core_pb2.SearchResponse:
        captured["scopes"] = scopes
        return core_pb2.SearchResponse()

    monkeypatch.setattr(core_client, "kb_search", fake_search)
    client.get("/search", params={"q": "x"})
    assert captured["scopes"] == ["public"]


def test_search_uses_principal_scopes_no_leak(monkeypatch) -> None:
    # The ⌘K palette must search under the viewer's scopes — bob (no group) must
    # never search group-scoped content.
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web, "_current_principal", lambda request: auth.Principal(viewer="bob", scopes=["public"])
    )
    captured: dict = {}

    async def fake_search(
        query: str, scopes: list[str], limit: int = 10
    ) -> core_pb2.SearchResponse:
        captured["scopes"] = scopes
        return core_pb2.SearchResponse()

    monkeypatch.setattr(core_client, "kb_search", fake_search)
    client.get("/search", params={"q": "secret"})
    assert captured["scopes"] == ["public"]
    assert "group" not in captured["scopes"]


def test_ask_history_accumulates_in_session(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake_ask(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
        return core_pb2.AskResponse(answer="ok", status=core_pb2.FOUND, tier="t", confidence=0.7)

    monkeypatch.setattr(core_client, "ask", fake_ask)
    c = TestClient(web.app)  # fresh client → clean session
    c.post("/ask", data={"q": "first question"})
    c.post("/ask", data={"q": "second question"})
    r = c.get("/")
    assert "first question" in r.text and "second question" in r.text
