"""Async gRPC client of the kernel Core API (`swarm.core.v1`).

Thin wrapper over `grpc.aio`. The channel speaks exactly the contract `cli_channel`
speaks (`swarm/cli` uses the sync stub; the web app is concurrent, so it uses the
native async stub — no threadpool bridge). It NEVER touches the graph DB: every
datum flows through a typed, scope-enforcing Core RPC (ADR-1).
"""

from __future__ import annotations

import os

from grpc import aio

from web_channel._gen import core_pb2, core_pb2_grpc


def core_addr() -> str:
    return os.environ.get("SWARM_CORE_ADDR", "127.0.0.1:50061")


def ask_timeout_s() -> float:
    """Deadline for an Ask. Ask is slow/bursty (LLM tiers / consilium), so the
    default is generous (5 min) — but bounded, so a hung kernel surfaces an honest
    error instead of an infinite spinner (brief A.0.3). Tune via SWARM_ASK_TIMEOUT_S."""
    return float(os.environ.get("SWARM_ASK_TIMEOUT_S", "300"))


def read_timeout_s() -> float:
    """Deadline for fast read RPCs (KbStatus/KbSearch — no LLM); bounded so a hung
    kernel can't stall a dashboard render (brief: no infinite spinner)."""
    return float(os.environ.get("SWARM_READ_TIMEOUT_S", "15"))


async def ask(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
    """Call Core.Ask. Raises grpc.aio.AioRpcError on an unreachable kernel or a
    DEADLINE_EXCEEDED — the route maps either to an honest `error` state (A.0.3)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.Ask(
            core_pb2.AskRequest(query=query, scopes=scopes, viewer=viewer),
            timeout=ask_timeout_s(),
        )


async def kb_status() -> core_pb2.StatusResponse:
    """Graph health + self-model (nodes/edges/inventory/namespaces/capabilities).
    Fast, no LLM — the dashboard 'state of my memory' tile."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.KbStatus(core_pb2.StatusRequest(), timeout=read_timeout_s())


async def kb_search(query: str, scopes: list[str], limit: int = 10) -> core_pb2.SearchResponse:
    """Scope-filtered retrieval over the graph (the ⌘K palette). Fast, no LLM."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.KbSearch(
            core_pb2.SearchRequest(query=query, scopes=scopes, limit=limit),
            timeout=read_timeout_s(),
        )


async def deliberation(
    ask_ref: str, scopes: list[str], viewer: str
) -> core_pb2.DeliberationResponse:
    """The retained panel-vs-judge deliberation behind a past escalated answer (ADR-15).
    Returned only to the owning viewer within scope; otherwise NOT_FOUND. Fast, no LLM."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.Deliberation(
            core_pb2.DeliberationRequest(ask_ref=ask_ref, scopes=scopes, viewer=viewer),
            timeout=read_timeout_s(),
        )


async def neighborhood(
    node_id: int,
    scopes: list[str],
    viewer: str,
    depth: int = 1,
    node_limit: int = 50,
    relation_types: list[str] | None = None,
) -> core_pb2.NeighborhoodResponse:
    """A bounded, scope-filtered neighborhood around one node (ADR-15) — the
    connections surface. Kernel clamps depth≤2/node_limit≤50. Fast, no LLM."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.Neighborhood(
            core_pb2.NeighborhoodRequest(
                node_id=node_id,
                depth=depth,
                node_limit=node_limit,
                relation_types=relation_types or [],
                scopes=scopes,
                viewer=viewer,
            ),
            timeout=read_timeout_s(),
        )


async def activity_feed(
    scopes: list[str],
    viewer: str,
    cursor: str = "",
    limit: int = 50,
    kinds: list[str] | None = None,
) -> core_pb2.ActivityFeedResponse:
    """One poll of the scope-safe worker/job activity log (ADR-15). The `cursor` is
    opaque (pass back a prior `next_cursor`; "" ⇒ most recent). Fast, no LLM."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ActivityFeed(
            core_pb2.ActivityFeedRequest(
                cursor=cursor, limit=limit, kinds=kinds or [], scopes=scopes, viewer=viewer
            ),
            timeout=read_timeout_s(),
        )
