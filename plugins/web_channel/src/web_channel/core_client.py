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


async def ask(
    query: str,
    scopes: list[str],
    viewer: str,
    active_keys: list[str] | None = None,
    conversation_id: str = "",
) -> core_pb2.AskResponse:
    """Call Core.Ask. Raises grpc.aio.AioRpcError on an unreachable kernel or a
    DEADLINE_EXCEEDED — the route maps either to an honest `error` state (A.0.3).
    `active_keys` (chat-thread epic 2): entity keys from the previous turn's
    citations — lets a pronoun follow-up ("its dependencies?") still hit the
    kernel's fast structured path. `conversation_id`: reserved for epic 3
    (conversation continuity); unused until `/ask` actually threads one turn's
    conversation into the next."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.Ask(
            core_pb2.AskRequest(
                query=query,
                scopes=scopes,
                viewer=viewer,
                active_keys=active_keys or [],
                conversation_id=conversation_id,
            ),
            timeout=ask_timeout_s(),
        )


async def kb_status() -> core_pb2.StatusResponse:
    """Graph health + self-model (nodes/edges/inventory/namespaces/capabilities).
    Fast, no LLM — the dashboard 'state of my memory' tile."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.KbStatus(core_pb2.StatusRequest(), timeout=read_timeout_s())


async def kb_search(
    query: str, scopes: list[str], limit: int = 10, assertion: str = ""
) -> core_pb2.SearchResponse:
    """Scope-filtered retrieval over the graph (the ⌘K palette). Fast, no LLM.
    `assertion` (ADR-16 D9): a signed actor assertion, verified + DERIVED
    kernel-side when present (wire `scopes` is then a legacy fallback only)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.KbSearch(
            core_pb2.SearchRequest(query=query, scopes=scopes, limit=limit, assertion=assertion),
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


async def rate_answer(
    ask_ref: str, scopes: list[str], viewer: str, rating: core_pb2.AnswerRating
) -> core_pb2.RateAnswerResponse:
    """Store the viewer's external rating for one answer. Fast, no LLM."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.RateAnswer(
            core_pb2.RateAnswerRequest(
                ask_ref=ask_ref,
                scopes=scopes,
                viewer=viewer,
                rating=rating,
            ),
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


# --- Identity / privacy (workspace ADR-16, step 6b) ------------------------
# These RPCs are ALWAYS strict (no legacy plaintext fallback): every call takes a
# signed actor assertion (`web_channel.actor.sign`); the kernel verifies it and
# derives the effective actor from its own records.


async def resolve_actor(assertion: str) -> core_pb2.ResolveActorResponse:
    """Verify a signed assertion and return the derived {uuid,scopes,caps,login}.
    Called once at login (the auth self-test / provisioning gate)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ResolveActor(
            core_pb2.ResolveActorRequest(assertion=assertion), timeout=read_timeout_s()
        )


async def provision_actor(provision: str) -> core_pb2.ResolveActorResponse:
    """JIT-provision (or re-sync) an SSO subject (ADR-16 D3): the whole claim set
    rides inside the signed provision token (`actor.sign_provision`). Called on
    every SSO login BEFORE resolve — creates the account for a new subject and
    re-syncs group membership for an existing one (an IdP group removal must
    propagate). The kernel guards resurrect/collision server-side."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ProvisionActor(
            core_pb2.ProvisionActorRequest(provision=provision), timeout=read_timeout_s()
        )


async def log_conversation(
    assertion: str,
    conversation_id: str = "",
    title: str = "",
    role: str = "",
    body: str = "",
    ask_ref: str = "",
) -> core_pb2.LogConversationResponse:
    """Log one turn (creates the conversation when `conversation_id` is empty;
    else appends to one the actor owns — NOT_FOUND if it is not theirs)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.LogConversation(
            core_pb2.LogConversationRequest(
                assertion=assertion,
                conversation_id=conversation_id,
                title=title,
                role=role,
                body=body,
                ask_ref=ask_ref,
            ),
            timeout=read_timeout_s(),
        )


async def list_conversations(assertion: str) -> core_pb2.ListConversationsResponse:
    """The actor's own conversations (owner-private), newest first."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ListConversations(
            core_pb2.ListConversationsRequest(assertion=assertion), timeout=read_timeout_s()
        )


async def list_users(
    assertion: str,
    include_deleted: bool = False,
    limit: int = 0,
    query: str = "",
    offset: int = 0,
) -> core_pb2.ListUsersResponse:
    """Kernel-truth user roster for admin pages and per-row actions. `query` is a
    server-side case-insensitive substring over login + names (kills the client
    ≤500 ceiling); `offset`/`limit` page it and the response `.total` is the
    pre-page match count for the pager."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ListUsers(
            core_pb2.ListUsersRequest(
                assertion=assertion,
                include_deleted=include_deleted,
                limit=limit,
                query=query,
                offset=offset,
            ),
            timeout=read_timeout_s(),
        )


async def get_user(assertion: str, user_id: str) -> core_pb2.GetUserResponse:
    """One user's full detail (identity, roles, groups, providers, emails) for the
    detail page — avoids scanning the roster at scale. NOT_FOUND for an unknown or
    tombstoned uuid. Same broad admin-cap gate as ListUsers."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.GetUser(
            core_pb2.GetUserRequest(assertion=assertion, user_id=user_id),
            timeout=read_timeout_s(),
        )


async def list_groups(assertion: str) -> core_pb2.ListGroupsResponse:
    """Read-only group list: name, member_count, granted_scopes, granted_roles."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ListGroups(
            core_pb2.ListGroupsRequest(assertion=assertion), timeout=read_timeout_s()
        )


async def get_group(assertion: str, group_id: str) -> core_pb2.GetGroupResponse:
    """One group with its members (login + providers + status) for the group detail
    page (ADR-19). NOT_FOUND for an unknown group; same admin-cap gate as ListGroups."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.GetGroup(
            core_pb2.GetGroupRequest(assertion=assertion, group_id=group_id),
            timeout=read_timeout_s(),
        )


async def list_roles(assertion: str) -> core_pb2.ListRolesResponse:
    """Read-only role list: name, capabilities, holder_count (fixed admin set)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ListRoles(
            core_pb2.ListRolesRequest(assertion=assertion), timeout=read_timeout_s()
        )


async def manage_group(
    assertion: str,
    op: core_pb2.GroupOp,
    group_id: str = "",
    name: str = "",
    description: str = "",
    role: str = "",
    scopes: list[str] | None = None,
    confirm: bool = False,
) -> core_pb2.AdminActionResponse:
    """First-class group lifecycle (ADR-18): create/rename/delete + set-role +
    set-scopes — capability-gated + audited kernel-side. `op` is a
    `core_pb2.GroupOp` value. Scopes are validated at the grant boundary (src:* /
    public only; private hard-denied). DELETE of a non-empty group needs confirm."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ManageGroup(
            core_pb2.ManageGroupRequest(
                assertion=assertion,
                op=op,
                group_id=group_id,
                name=name,
                description=description,
                role=role,
                scopes=scopes or [],
                confirm=confirm,
            ),
            timeout=read_timeout_s(),
        )


async def list_sso_map(assertion: str) -> core_pb2.ListSsoMapResponse:
    """Incoming-SSO-group → our-group mappings (ADR-18 ps-4), ordered by provider
    then incoming group. `manage_access`-gated kernel-side."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ListSsoMap(
            core_pb2.ListSsoMapRequest(assertion=assertion), timeout=read_timeout_s()
        )


async def manage_sso_map(
    assertion: str,
    op: core_pb2.SsoMapOp,
    provider: str = "",
    incoming_group: str = "",
    our_group_id: str = "",
) -> core_pb2.AdminActionResponse:
    """Upsert/delete an SSO group mapping (ADR-18 ps-4) — `manage_access`-gated +
    audited. `op` is a `core_pb2.SsoMapOp` value (SSO_MAP_PUT/SSO_MAP_DELETE). PUT
    onto a non-existent group is BAD_REQUEST; default-deny stays (unmapped grants
    nothing)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ManageSsoMap(
            core_pb2.ManageSsoMapRequest(
                assertion=assertion,
                op=op,
                provider=provider,
                incoming_group=incoming_group,
                our_group_id=our_group_id,
            ),
            timeout=read_timeout_s(),
        )


async def get_conversation(
    assertion: str, conversation_id: str
) -> core_pb2.GetConversationResponse:
    """One conversation the actor owns, with its messages; NOT_FOUND otherwise
    (404-not-403 — not-owned and non-existent are indistinguishable)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.GetConversation(
            core_pb2.GetConversationRequest(assertion=assertion, conversation_id=conversation_id),
            timeout=read_timeout_s(),
        )


async def admin_read_conversation(
    assertion: str, conversation_id: str, reason: str
) -> core_pb2.GetConversationResponse:
    """Break-glass: a superadmin reads another user's conversation by impersonating
    the owner through the same predicate, audited BEFORE return (ADR-16 D6).
    `reason` is required — an empty reason is a bad request, not an unlogged read."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.AdminReadConversation(
            core_pb2.AdminReadConversationRequest(
                assertion=assertion, conversation_id=conversation_id, reason=reason
            ),
            timeout=read_timeout_s(),
        )


async def manage_access(
    assertion: str,
    op: core_pb2.AccessOp,
    target_user_id: str = "",
    role: str = "",
    group_id: str = "",
    scopes: list[str] | None = None,
) -> core_pb2.AdminActionResponse:
    """Grant/revoke a role or group, or set a group's scopes (ADR-16 D10) —
    capability-gated + audited kernel-side. `op` is a `core_pb2.AccessOp` value
    (GRANT_ROLE/REVOKE_ROLE/GRANT_GROUP/REVOKE_GROUP/SET_GROUP_SCOPES)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ManageAccess(
            core_pb2.ManageAccessRequest(
                assertion=assertion,
                op=op,
                target_user_id=target_user_id,
                role=role,
                group_id=group_id,
                scopes=scopes or [],
            ),
            timeout=read_timeout_s(),
        )


async def manage_user(
    assertion: str,
    op: core_pb2.UserOp,
    target_user_id: str = "",
    login: str = "",
    first_name: str = "",
    last_name: str = "",
    nickname: str = "",
    external: bool = False,
) -> core_pb2.AdminActionResponse:
    """User lifecycle: invite / deactivate / delete (ADR-16 D11) — capability-gated
    + audited. `op` is a `core_pb2.UserOp` value (INVITE/DEACTIVATE/DELETE).
    `external=True` invites a GUEST (ADR-20: no default cohort, no internal visibility
    until a Project admits them). Deactivate/delete kill the login immediately; learned
    content persists. Any lifecycle op on a Wheel member needs an elevation kernel-side."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ManageUser(
            core_pb2.ManageUserRequest(
                assertion=assertion,
                op=op,
                target_user_id=target_user_id,
                login=login,
                first_name=first_name,
                last_name=last_name,
                nickname=nickname,
                external=external,
            ),
            timeout=read_timeout_s(),
        )


# --- Projects + elevation (workspace ADR-20) ----------------------------------


async def list_projects(assertion: str, mine_only: bool = False) -> core_pb2.ListProjectsResponse:
    """Projects the actor can see: an admin cap sees every Project (metadata), anyone else
    exactly the Projects they are a member of or that are public."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ListProjects(
            core_pb2.ListProjectsRequest(assertion=assertion, mine_only=mine_only),
            timeout=read_timeout_s(),
        )


async def get_project(assertion: str, project_id: str) -> core_pb2.GetProjectResponse:
    """One Project with its Sources (stable `src:<uuid>` scopes) and members; NOT_FOUND for
    a non-member (404-not-403) or an unknown id."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.GetProject(
            core_pb2.GetProjectRequest(assertion=assertion, project_id=project_id),
            timeout=read_timeout_s(),
        )


async def manage_project(
    assertion: str,
    op: core_pb2.ProjectOp,
    project_id: str = "",
    name: str = "",
    description: str = "",
    visibility: str = "",
    source_id: str = "",
    source_kind: str = "",
    source_label: str = "",
    member_user_id: str = "",
    member_group_id: str = "",
    member_role: str = "",
    confirm: bool = False,
) -> core_pb2.AdminActionResponse:
    """Project lifecycle / Sources / membership (ADR-20 §6) — capability-gated + audited
    kernel-side (publicness needs an elevation; a self-grant needs the owner or an
    elevation). `op` is a `core_pb2.ProjectOp` value."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.ManageProject(
            core_pb2.ManageProjectRequest(
                assertion=assertion,
                op=op,
                project_id=project_id,
                name=name,
                description=description,
                visibility=visibility,
                source_id=source_id,
                source_kind=source_kind,
                source_label=source_label,
                member_user_id=member_user_id,
                member_group_id=member_group_id,
                member_role=member_role,
                confirm=confirm,
            ),
            timeout=read_timeout_s(),
        )


async def elevate(
    assertion: str, reason: str, reauth: str, ttl_s: int = 0
) -> core_pb2.ElevateResponse:
    """Request a time-boxed superadmin elevation (ADR-20 D9): a local Wheel member presents
    a reason and the channel-signed re-auth proof (`actor.sign_reauth`, minted only after a
    fresh password check). The kernel audits BEFORE the capability exists and binds it to
    the assertion's session. NOT_AUTHORIZED (not Wheel / not local), BAD_REQUEST (reason /
    proof), OK with the expiry."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.Elevate(
            core_pb2.ElevateRequest(assertion=assertion, reason=reason, reauth=reauth, ttl_s=ttl_s),
            timeout=read_timeout_s(),
        )


async def end_elevation(assertion: str, elevation_id: str = "") -> core_pb2.AdminActionResponse:
    """End (revoke) the actor's live elevation for this session early ("" = the current one)."""
    async with aio.insecure_channel(core_addr()) as channel:
        stub = core_pb2_grpc.CoreStub(channel)
        return await stub.EndElevation(
            core_pb2.EndElevationRequest(assertion=assertion, elevation_id=elevation_id),
            timeout=read_timeout_s(),
        )
