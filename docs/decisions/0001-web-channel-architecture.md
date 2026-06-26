# ADR-1 (hive): web_channel — gRPC Core client, new-surfaces-are-new-RPCs, Keycloak identity

## Status

Proposed (architect plan `hive/docs/design/web_channel-plan.md`; goes to an independent
critic before any build)

## Context

The Product Owner brief (`board/ideas/hive-chat-channel.md`) wants a web operator console —
`web_channel`, the web sibling of `cli_channel`. A server-side web app is *tempted* to read
the graph Postgres directly (fast dashboards/feeds/graph), and to invent its own auth. Both
would be load-bearing mistakes: a DB reach-around bypasses kernel scope-enforcement,
presentation-determinism, and the boring-kernel rule; a homegrown auth re-implements identity
the org already runs. The PO has also relaxed the brief's "single operator / no auth" non-goal:
real testers with **differentiated Confluence access** are coming, which makes scope/privacy
no-leak a live multi-user test and requires a real IdP.

## Decision

1. **web_channel is a gRPC client of the Core API (`swarm.core.v1`), an out-of-process hive
   plugin** (Python sidecar; HTMX server-side render). It speaks exactly the contract
   `cli_channel` speaks — no web-specific kernel surface.
2. **A new UI surface ⇒ a new typed Core RPC. The channel NEVER reads the graph DB.** Every
   datum flows through a scope-enforcing Core RPC. New RPCs (Evidence / Brief / ActivityFeed /
   Neighborhood) extend `core.proto`; they are swarm/ dependencies, sequenced per the plan.
3. **Identity is Keycloak (OIDC).** An authenticated session maps to the kernel's `viewer` +
   allowed `scopes` (from Keycloak groups/roles). **The kernel remains the sole
   scope-enforcement authority** — the channel only passes the authenticated identity; it
   never decides visibility. The coarse scope case needs no kernel change (the kernel already
   accepts `viewer`+`scopes`).
4. **Rendering is deterministic** (presentation-determinism): no model chooses formatting or
   re-spells any value/id/link/citation; persona/i18n re-phrase from structured fields only.

OPEN (the §7 fork, not locked here): the **cohort-2 ACL granularity** — whether "may know the
info but not access the source" stays coarse (public-only) or becomes a fine per-source ACL
with citation redaction (a swarm/ kernel change). Decided before P3.

## Consequences

- The kernel gains a few small, typed, **scope-enforcing** read RPCs; scope/privacy lives in
  ONE place (the kernel), never re-implemented in Python. The channel stays a thin renderer.
- Auth-beyond-local + multi-user + light RBAC come INTO scope (PO relaxed the brief non-goal);
  the privacy no-leak invariant becomes a **live adversarial test by real testers** (plan P1).
- P0 ships channel-only (existing `Ask`) with a fixed operator before Keycloak lands.

## Alternatives rejected

- **Direct DB access from the channel** — bypasses scope-enforcement + determinism + boring
  kernel; the single worst move for a web app over a private graph.
- **A kernel-side HTML/REST surface** — the kernel stops being boring and couples to render.
- **Homegrown auth** — re-implements identity the org runs in Keycloak; weaker, more to get wrong.
