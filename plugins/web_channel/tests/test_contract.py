"""Contract test: a real grpc.aio round-trip against an in-process stub Core
server. Proves the generated stubs + core_client wire up to the actual proto
contract (field names/types), independent of the web layer or a live kernel.
"""

from __future__ import annotations

import asyncio

import grpc
import pytest
from grpc import aio

from web_channel import core_client
from web_channel._gen import core_pb2, core_pb2_grpc


class _StubCore(core_pb2_grpc.CoreServicer):
    async def Ask(
        self, request: core_pb2.AskRequest, context: grpc.aio.ServicerContext
    ) -> core_pb2.AskResponse:
        # Echo the request back through the response so we can assert the
        # request fields crossed the wire as sent.
        return core_pb2.AskResponse(
            answer=f"echo:{request.query}|{','.join(request.scopes)}|{request.viewer}",
            confidence=0.91,
            tier="tier_tools",
            status=core_pb2.FOUND,
            citations=[core_pb2.Citation(source="stub", ref=request.query, confidence=0.5)],
        )

    async def ListUsers(
        self, request: core_pb2.ListUsersRequest, context: grpc.aio.ServicerContext
    ) -> core_pb2.ListUsersResponse:
        assert request.assertion == "assertion-1"
        assert request.include_deleted is True
        assert request.limit == 7
        return core_pb2.ListUsersResponse(
            status=core_pb2.CALL_OK,
            users=[
                core_pb2.UserView(
                    id="12345678-aaaa-bbbb-cccc-123456789abc",
                    login="alice",
                    first_name="Alice",
                    status="active",
                    roles=["admin"],
                    groups=["confluence"],
                    providers=["local"],
                    last_login_at="2026-07-08T10:00:00Z",
                )
            ],
        )


async def test_ask_round_trips_against_real_grpc_server(monkeypatch) -> None:
    server = aio.server()
    core_pb2_grpc.add_CoreServicer_to_server(_StubCore(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        monkeypatch.setenv("SWARM_CORE_ADDR", f"127.0.0.1:{port}")
        resp = await core_client.ask("hello", ["public", "group"], "operator")
        assert resp.status == core_pb2.FOUND
        assert resp.confidence == 0.91
        # request fields made it across exactly as sent
        assert resp.answer == "echo:hello|public,group|operator"
        assert resp.citations[0].source == "stub"
        assert resp.citations[0].ref == "hello"
    finally:
        await server.stop(None)


async def test_list_users_round_trips_against_real_grpc_server(monkeypatch) -> None:
    server = aio.server()
    core_pb2_grpc.add_CoreServicer_to_server(_StubCore(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        monkeypatch.setenv("SWARM_CORE_ADDR", f"127.0.0.1:{port}")
        resp = await core_client.list_users("assertion-1", include_deleted=True, limit=7)
        assert resp.status == core_pb2.CALL_OK
        assert resp.users[0].login == "alice"
        assert resp.users[0].roles == ["admin"]
    finally:
        await server.stop(None)


class _SlowCore(core_pb2_grpc.CoreServicer):
    async def Ask(
        self, request: core_pb2.AskRequest, context: grpc.aio.ServicerContext
    ) -> core_pb2.AskResponse:
        await asyncio.sleep(5)  # longer than the test deadline below
        return core_pb2.AskResponse(status=core_pb2.FOUND)


async def test_ask_deadline_surfaces_as_rpc_error(monkeypatch) -> None:
    # A hung kernel must surface DEADLINE_EXCEEDED (→ honest error card), never hang
    # forever (brief A.0.3 "no infinite spinner").
    server = aio.server()
    core_pb2_grpc.add_CoreServicer_to_server(_SlowCore(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        monkeypatch.setenv("SWARM_CORE_ADDR", f"127.0.0.1:{port}")
        monkeypatch.setenv("SWARM_ASK_TIMEOUT_S", "0.3")
        with pytest.raises(aio.AioRpcError) as exc:
            await core_client.ask("slow", ["public"], "operator")
        assert exc.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
    finally:
        await server.stop(None)
