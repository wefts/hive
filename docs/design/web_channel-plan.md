---
date: 2026-06-25
status: Plan — draft for independent critic (architect output; not yet built)
owner: hive
relates-to: board/todo/hive-chat-channel (sibling PUBLIC chat channel; shares the T9 persona skill — not superseded)
brief: board/ideas/hive-chat-channel.md (Product Owner brief)
---

# web_channel — architect plan

Turns the Product Brief (`board/ideas/hive-chat-channel.md`) into a reviewable plan.
**No implementation code here.** Honors `docs/architecture/ports.md`,
`docs/standards/presentation-determinism.md`, `docs/standards/verification.md`, and the
`swarm/proto/core.proto` contract. Goes to an independent critic before any build.

## 1. Attachment — how web_channel sits behind the channel port

`web_channel` is a **`channel` adapter** (ports.md): an *interaction surface*, the web
sibling of `cli_channel`. Both speak the **same outward contract — the gRPC Core API**
(`swarm/proto/core.proto`, `package swarm.core.v1`), exactly as the CLI does (`swarm/cli`
is already a Python grpcio client of it).

- **Runtime mode:** out-of-process **sidecar/container** (ports.md) — a small Python web
  app in `hive/plugins/web_channel/`, its own Compose service, a gRPC client of the
  kernel's Core API (`:50061`). It requires **zero kernel code change to exist** (Phase 0).
- **Rendering:** **server-side HTML** (Python → HTMX partials); Alpine for local
  interactivity; Tailwind + Basecoat tokens; dark mode. **No model in the render path.**
- **Identity & access (P1):** the channel sits behind **Keycloak (OIDC)**; an authenticated
  session maps to the kernel's `viewer` + allowed `scopes` (from Keycloak groups/roles).
  The kernel stays the scope-enforcement authority — the channel only *passes* the
  authenticated identity, it never decides visibility. (Coarse case = no kernel change; the
  fine cohort-2 ACL is the §7 fork.)
- **Manifest** (ports.md minimum): name `web_channel`, kind `channel`, runtime
  sidecar, entrypoint, protocol version (`swarm.core.v1`), env (Core API address, viewer
  identity mapping), capabilities (the RPCs it consumes), safety class `read-only`
  (Phase 0–2 issue no graph writes).

### The load-bearing invariant (ADR-worthy — §4)

**The channel never touches the graph DB. Every datum it shows comes through a typed Core
RPC that enforces scope.** A Python web app will be *tempted* to `SELECT` from Postgres for
a dashboard/feed/graph — that would bypass scope-enforcement, presentation-determinism, and
the boring-kernel rule in one move. So: **a new surface ⇒ a new typed Core RPC**, never a
DB reach-around.

## 2. The channel↔kernel contract — what EXISTS vs what's a swarm/ dependency

`core.proto v1` today exposes exactly three RPCs. This cleanly partitions the brief:

| Brief surface | Needs | Status |
| --- | --- | --- |
| **Ask → honest answer** (status/confidence/citations) | `Ask` | ✅ **EXISTS** — `Ask(query,scopes,viewer)→{answer,confidence,tier,citations[source/ref/confidence],status}` |
| **⌘K search / jump to entity** | `KbSearch` | ✅ **EXISTS** — `KbSearch(query,scopes,limit)→hits[id,type,key,score]` |
| **"State of my memory" tile** | `KbStatus` | ✅ **EXISTS** — `KbStatus()→{nodes,edges,namespaces,inventory[TypeCount],last_activity,capabilities}` |
| **Evidence drill-down** (origins, *independent* corroboration, folded dups, consilium dissent) | NEW `Evidence` RPC | ⛔ **swarm/ DEP** — the ADR-13 data exists in the graph (`origin`, `seen_count=distinct-origin`, `entity_resolution_audit`, `combine_typed`, consilium votes) but **`Citation` carries only source/ref/confidence** — no origin/corroboration/dissent on the wire |
| **Rich dashboard tiles** (what-changed, open-questions, low-confidence/needs-you) | NEW `Brief` RPC | ⛔ **swarm/ DEP** — needs self-model known-unknowns + per-domain confidence + a change/delta query; none exposed today |
| **Live activity feed** (worker traces) | NEW `ActivityFeed` RPC | ⛔ **swarm/ DEP** *and* depends on workers actually running (see §6 disagreement) |
| **Local graph neighborhood** | NEW `Neighborhood` RPC | ⛔ **swarm/ DEP** — kernel has `Traverse`; not exposed; expose a **bounded** (depth≤2, limit, link-type filter, scoped) neighborhood |

**Proposed new Core RPCs (swarm/, extend `core.proto`; each scope-enforced, viewer-aware):**

- `Evidence(EvidenceRequest{node_id|claim_ref, scopes, viewer}) → {origins[], independent_origin_count, folded_duplicates[{merged_into, n}], consilium_votes[{model, verdict}]}` — exposes ADR-13 origin/corroboration + resolution folds + dissent. **The single highest-value kernel dep** (it's what makes trust legible).
- `Brief(BriefRequest{scopes, viewer}) → {recent_changes[], open_questions[], low_confidence_areas[], counts}` — the home-brief tiles beyond what `KbStatus` covers.
- `ActivityFeed(FeedRequest{since_cursor, scopes}) → traces[{worker, kind, node_ref, at}]` — **poll**, not stream (HTMX refresh), for local-first simplicity.
- `Neighborhood(NeighborhoodRequest{node_id, depth≤2, link_types[], limit, scopes}) → {nodes[], edges[]}`.

## 3. Phase plan (criteria up front · external signals · manual QA · cut line)

> **Sequencing (PO decisions, 2026-06-25).** The PO chose the brief's *feature* order
> (dashboard before evidence) AND added two foundational requirements that come FIRST: real
> auth via **Keycloak (OIDC)** and **real testers with differentiated Confluence access**.
> So **auth + the live privacy-no-leak gate (P1) precede the feature phases.** This relaxes
> the brief's "single operator / no multi-user / no auth beyond local" non-goal (§6 — a
> deliberate PO change). Persona/i18n: English kernel default first; T9 UA/FR re-phrasing later.

### P0 — Walking skeleton: one honest answer (channel-only; `Ask`)

- **Criteria:** brief A.0.1–A.0.4 (found+citations verbatim; not_found honest; kernel-down→error; `[<&\`` survive escaping).
- **External signals:** channel unit tests render each `AnswerStatus` deterministically from a fixture `AskResponse`; an escaping test on adversarial citation `ref`; `docker compose config` valid; a contract test against a stub Core server.
- **Manual QA:** brief §0 script (ask answerable → found+citation spot-checked char-for-char; ask out-of-scope → dignified not_found; stop kernel → error; special-char ref verbatim).
- **Cut line:** a single input box + answer card (status badge, confidence, citation chips), a **fixed operator viewer, no auth yet**. The daily-usable query surface — ship it first.

### P1 — Keycloak identity + scope mapping + the live privacy-no-leak gate (FOUNDATIONAL)

- **Why first:** real testers with real (lack of) Confluence access turn scope/privacy
  no-leak — the ONE hard invariant — into a **live adversarial test by real humans**, not a
  unit test. You cannot put differentiated-access testers on the system without auth.
- **Identity:** web_channel sits behind **Keycloak (OIDC)**; a session resolves to a
  canonical `viewer` + the allowed **scopes** derived from Keycloak groups/roles (e.g. a
  `confluence` group → the `group` scope). Every `Ask`/`KbSearch` carries them. The kernel
  already accepts `viewer`+`scopes` (`AskRequest`), so the **coarse** case is channel +
  Keycloak deployment — **no kernel change**.
- **The two PO tester cohorts:** (1) **no Confluence access, must never get it** → the hard
  no-leak gate (existing default-deny scope model). (2) **no direct access but may
  legitimately KNOW info from it** → the **ACL-granularity fork (§7):** coarse (cohort-2
  sees only `public`-scoped derived knowledge) vs fine (derived knowledge visible, the
  source **citation redacted** — a swarm/ kernel ACL change).
- **Criteria / HARD GATE:** a cohort-1 tester NEVER receives Confluence content via answer
  prose, citations, `KbSearch` hits, or (later) evidence drill-down — proven by **real
  testers running adversarial queries**, not output inspection alone (brief A.2.6 + §8 checklist).
- **Signals:** Keycloak in compose; Keycloak-group→scope mapping tests; an adversarial scope
  suite run under each tester identity; Keycloak secrets/realm config out of public repos.
- **Cut line:** Keycloak login → scoped `Ask`; the cohort-1 no-leak gate **green with real testers**.

### P2 — Home-brief dashboard (PO feature-first; `KbStatus` now + new `Brief` RPC)

- **Criteria:** cold open lands on the dashboard, no faked tiles (A.1.1/A.1.4); a real
  **"state of memory"** tile from `KbStatus`; the richer tiles (what-changed / open-questions
  / low-confidence-needs-you) from a new **`Brief` RPC** (swarm dep) or **absent/"not
  available"** until it lands; ⌘K search (`KbSearch`); core loop keyboard-only (A.1.2/A.1.3).
- **Signals:** tile asserts against `KbStatus`/`Brief` fixtures; empty-state test; keyboard e2e.
- **Cut line:** dashboard with the `KbStatus` tile + ⌘K + session history; `Brief`-backed tiles ship when the RPC does.

### P3 — Evidence & trust (swarm dep: `Evidence` RPC) + per-access citation handling

- **Criteria:** brief A.2.1–A.2.6 — drill to origin(s); corroboration = **independent**
  origin count (a re-emission does NOT inflate); single-source labelled; folded dups visible
  as folded; consilium dissent shown; **private-scope source never leaks (hard gate)**. The
  cohort-2 rule from §7 lands here (redact vs withhold private citations).
- **Signals:** kernel `Evidence` RPC tests (independent-origin count; folded-dup; scope
  filter + cohort-2 redaction); channel renders the panel deterministically; adversarial
  scope test under both tester cohorts.
- **Cut line:** from a citation, a drill panel showing origins + independent count + folded + dissent.

### P4 — Activity feed + local graph (swarm deps: `ActivityFeed`, `Neighborhood`)

- **DEFERRED until the cognitive loop runs** (§6 #2): a feed of worker traces is empty while
  enrichment/ER are off-by-default. Local graph = bounded `Neighborhood` (≤50 nodes,
  link-type filter, never a full-graph hairball — A.3.2). Poll, not stream (§6 #3).

## 4. ADR stub (Proposed) — record before build

**hive ADR: web_channel is a gRPC client of the Core API; new surfaces are new Core RPCs; the channel never reads the graph DB.**

- *Context:* a server-side web app could query Postgres directly for tiles/feed/graph — fast, and catastrophic to scope-enforcement + determinism + boring-kernel.
- *Decision:* `web_channel` consumes only `swarm.core.v1` (gRPC); every dynamic datum flows through a typed, scope-enforcing Core RPC; rendering is deterministic server-side HTML (no model in the render path); persona/i18n re-phrase from fields only (presentation-determinism). New surfaces ⇒ extend `core.proto`, never a DB reach-around.
- *Consequences:* the kernel gains a few read RPCs (Evidence/Brief/Feed/Neighborhood) — small, typed, scoped; the channel stays a thin renderer; scope/privacy is enforced in ONE place (the kernel), not re-implemented in Python.
- *Alternatives rejected:* direct DB access (bypasses scope+determinism); a kernel-side HTML/REST surface (kernel stops being boring; couples render to kernel).

## 5. Dependency-ordered sequence (so no tile is ever faked)

```text
channel-only (existing RPCs):   P0 (Ask)                                    ← ship now; daily-usable
foundational auth + privacy:    P1 Keycloak + scope mapping + LIVE no-leak gate (real testers)
swarm dep, dashboard:           P2 needs Brief RPC (KbStatus tile ships now)
swarm dep, highest-value trust:  P3 needs Evidence RPC (+ cohort-2 citation rule, maybe ACL change)
swarm dep + loop running:        P4 needs ActivityFeed + Neighborhood RPCs  ← gated on the cognitive loop
```

Kernel-dependent phases are gated on their RPC landing in `core.proto`; until then the
surface is **absent or "not available," never faked** (brief A.1.4). P1 (Keycloak + the
privacy gate) is foundational — it precedes the feature phases because real testers can't be
admitted without auth, and the no-leak gate is the project's one hard invariant.

## 6. Where I disagree with the brief (verification.md: disagreement is signal)

1. **Phase order — RAISED, PO chose dashboard-first.** I argued for conversation + evidence
   before the dashboard (it fits "get to know the swarm by conversing"); the PO chose the
   brief's feature order (dashboard before evidence). Recorded and honored — features run
   P2 dashboard → P3 evidence. (Auth + the privacy gate still precede both, P1.)
2. **The activity feed (now P4) has nothing to show yet.** Worker traces require the
   enrichment/ER loop *running*, and it is off-by-default / set aside. P4 stays **deferred
   until the cognitive loop is turned on** — otherwise it's a live feed of an idle system.
3. **"Live" feed → poll, not stream.** Streaming/websockets fights local-first simplicity;
   an HTMX poll/refresh digest meets "near-live" without the machinery.
4. **Multi-user / auth was a brief NON-GOAL — the PO has now relaxed it.** Keycloak + real
   differentiated-access testers bring auth-beyond-local + multi-user + (light) RBAC INTO
   scope. Recorded as a deliberate brief change, not smoothed over; the brief's §6 non-goal
   should be updated.

## 7. PO decisions (resolved 2026-06-25) + the one open fork

- **Phase priority:** dashboard-first (the brief's feature order). ✓
- **Identity:** **Keycloak (OIDC)** — real login/session→viewer now, not a fixed operator. ✓
- **Persona/i18n:** English kernel default first; T9 UA/FR re-phrasing later (folds in the
  older `board/todo/hive-chat-channel` persona stub). ✓
- **Real testers:** the PO can supply real testers in two cohorts (no Confluence access /
  no-direct-access-but-may-know) — making the privacy no-leak gate (P1) a live test. ✓

**THE ONE OPEN FORK — ACL granularity for cohort-2** ("no direct Confluence access, but may
legitimately KNOW info from it"):

- **(A) Coarse, no kernel change.** Cohort-2 gets only `public`-scoped answers/knowledge;
  anything `group`/private is default-denied. Simple, ships with P1. Cost: cohort-2 can't see
  Confluence-*derived* knowledge unless it was explicitly declassified to `public`.
- **(B) Fine, a swarm/ kernel change.** Per-source/per-claim ACL: cohort-2 sees the
  *derived knowledge / answer* while the **citation to the private source is redacted**
  ("source restricted"). Closer to "may know the info but not access the source," but it is a
  real kernel ACL + citation-redaction change (and a declassification-policy question: which
  derived claims become knowable). Defer to a dedicated phase.

Recommendation: **start (A) coarse** so P1 + the cohort-1 hard gate ship now; treat (B) as a
flagged follow-on phase if cohort-2's "knowable-but-not-accessible" need is real in practice.
