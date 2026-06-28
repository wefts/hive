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


async def ask(query: str, scopes: list[str], viewer: str) -> core_pb2.AskResponse:
    """Call Core.Ask. Raises grpc.aio.AioRpcError on an unreachable kernel or a
    DEADLINE_EXCEEDED — the route maps either to an honest `error` state (A.0.3)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.Ask(
            core_pb2.AskRequest(query=query, scopes=scopes, viewer=viewer),
            timeout=ask_timeout_s(),
        )
