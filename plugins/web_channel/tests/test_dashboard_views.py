"""Dashboard views (ADR-15): deliberation (panel-vs-judge), neighborhood (bounded
connections), activity (polled feed). Driven through the app with the Core client
faked (no live kernel). Asserts real render, honest absent states, scope/viewer
passthrough, and the opaque-cursor poll loop."""

from __future__ import annotations

from fastapi.testclient import TestClient

from web_channel import auth, core_client
from web_channel import main as web
from web_channel._gen import core_pb2

client = TestClient(web.app)


# --- deliberation ----------------------------------------------------------


def test_deliberation_renders_panel_and_judge(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake(ask_ref: str, scopes: list[str], viewer: str):
        return core_pb2.DeliberationResponse(
            status=core_pb2.FOUND,
            ask_ref=ask_ref,
            answer="The synthesized single voice.",
            confidence=0.82,
            disagreement=0.25,
            panel=[
                core_pb2.PanelTake(model="qwen3:14b", answer="take A"),
                core_pb2.PanelTake(model="gemma4:e4b", answer="take B"),
            ],
            judge="gemma4:31b",
            created_at="2026-06-29T10:00:00Z",
        )

    monkeypatch.setattr(core_client, "deliberation", fake)
    r = client.get("/deliberation/opaqueref123")
    assert r.status_code == 200
    assert "qwen3:14b" in r.text and "take A" in r.text  # panel takes shown
    assert "gemma4:31b" in r.text  # judge
    assert "synthesized single voice" in r.text  # judge synthesis
    assert "75%" in r.text  # agreement = 1 - 0.25


def test_deliberation_not_found_is_honest_not_error(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake(ask_ref: str, scopes: list[str], viewer: str):
        return core_pb2.DeliberationResponse(status=core_pb2.NOT_FOUND)

    monkeypatch.setattr(core_client, "deliberation", fake)
    r = client.get("/deliberation/expired")
    assert r.status_code == 200
    assert "no longer available" in r.text.lower()
    assert "qwen" not in r.text  # no panel leaked on a non-FOUND


def test_deliberation_passes_session_scope_and_viewer(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        web,
        "_current_principal",
        lambda req: auth.Principal(viewer="alice", scopes=["public", "group"]),
    )
    seen: dict = {}

    async def fake(ask_ref: str, scopes: list[str], viewer: str):
        seen["scopes"], seen["viewer"] = scopes, viewer
        return core_pb2.DeliberationResponse(status=core_pb2.NOT_FOUND)

    monkeypatch.setattr(core_client, "deliberation", fake)
    client.get("/deliberation/x")
    assert seen == {"scopes": ["public", "group"], "viewer": "alice"}


# --- neighborhood ----------------------------------------------------------


def test_neighborhood_renders_nodes_edges_and_filter(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake(node_id, scopes, viewer, depth=1, node_limit=50, relation_types=None):
        return core_pb2.NeighborhoodResponse(
            status=core_pb2.FOUND,
            center_id=node_id,
            nodes=[
                core_pb2.NodeView(id=2, type="article", key="Motörhead", scope="public", depth=1)
            ],
            edges=[
                core_pb2.EdgeView(src_id=node_id, dst_id=2, relation="mentions", reliability=0.9)
            ],
            truncated=True,
        )

    monkeypatch.setattr(core_client, "neighborhood", fake)
    r = client.get("/neighborhood/1")
    assert r.status_code == 200
    assert "Motörhead" in r.text and "mentions" in r.text
    assert "truncated" in r.text.lower()  # honest bound
    assert "/neighborhood/1?rel=mentions" in r.text  # relation-type filter chip


def test_neighborhood_not_found_is_honest_empty(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake(node_id, scopes, viewer, depth=1, node_limit=50, relation_types=None):
        return core_pb2.NeighborhoodResponse(status=core_pb2.NOT_FOUND, center_id=node_id)

    monkeypatch.setattr(core_client, "neighborhood", fake)
    r = client.get("/neighborhood/999")
    assert r.status_code == 200
    assert "no connections" in r.text.lower() or "visible to you" in r.text.lower()


def test_neighborhood_forwards_relation_filter(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)
    seen: dict = {}

    async def fake(node_id, scopes, viewer, depth=1, node_limit=50, relation_types=None):
        seen["rel"] = relation_types
        return core_pb2.NeighborhoodResponse(status=core_pb2.FOUND, center_id=node_id)

    monkeypatch.setattr(core_client, "neighborhood", fake)
    client.get("/neighborhood/1", params={"rel": "mentions,cites"})
    assert seen["rel"] == ["mentions", "cites"]


# --- activity --------------------------------------------------------------


def test_activity_renders_events_and_opaque_poller(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake(scopes, viewer, cursor="", limit=50, kinds=None):
        return core_pb2.ActivityFeedResponse(
            status=core_pb2.FOUND,
            events=[
                core_pb2.ActivityEvent(
                    kind="node_added", at="2026-06-29T10:00:00Z", subject_type="article"
                )
            ],
            next_cursor="OPAQUE-NEXT",
        )

    monkeypatch.setattr(core_client, "activity_feed", fake)
    r = client.get("/activity", params={"cursor": ""})
    assert r.status_code == 200
    assert "node_added" in r.text and "article" in r.text
    # the OOB poller carries the opaque next_cursor for the next tick
    assert 'hx-swap-oob="true"' in r.text
    assert "cursor=OPAQUE-NEXT" in r.text
    assert "every 6s" in r.text


def test_activity_empty_keeps_polling(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: False)

    async def fake(scopes, viewer, cursor="", limit=50, kinds=None):
        return core_pb2.ActivityFeedResponse(status=core_pb2.NOT_FOUND, next_cursor="TAILCUR")

    monkeypatch.setattr(core_client, "activity_feed", fake)
    r = client.get("/activity", params={"cursor": "prev"})
    assert r.status_code == 200
    assert "cursor=TAILCUR" in r.text  # still re-arms the poller


def test_activity_dead_session_stops_polling(monkeypatch) -> None:
    monkeypatch.setattr(auth, "oidc_enabled", lambda: True)
    monkeypatch.setattr(web, "_current_principal", lambda req: None)
    r = client.get("/activity")
    assert r.status_code == 200
    # The poller is OOB-replaced with a disarmed (trigger-less) span → the loop stops.
    assert "every 6s" not in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert "log in" in r.text.lower()
