---
status: draft
adr: hive ADR-3 (Proposed)
owns: hive web_channel — templates/CSS/JS only for epic 1; kernel touch-points noted for epics 2/3
supersedes: nothing (extends the 2026-06-29 microblog redesign, board/journal.md)
---

# Chat thread UI — design spec

Implements hive ADR-3. Three gated epics; this spec covers epic 1 fully and
sketches 2/3 as forward-looking notes (not committed designs — decide those when
reached).

## Epic 1 — visual thread (this spec, ready to build)

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ topbar (unchanged: brand · nav · ⌘K · principal · logout)   │
├───────────────┬───────────────────────────────────────────────┤
│ aside-col      │ .thread  (scrollable, flex-column, oldest→newest) │
│ ┌────────────┐│ ┌───────────────────────────────────────────┐│
│ │ State of   ││ │ .thread-post.user                         ││
│ │ my memory  ││ │  you · 14:02                               ││
│ │ (KbStatus, ││ │  What is Keycloak used for?                ││
│ │  unchanged)││ └───────────────────────────────────────────┘│
│ └────────────┘│ ┌───────────────────────────────────────────┐│
│                │ │ .thread-post.assistant                    ││
│  ("Recent      │ │  swarm · found · confidence 0.91           ││
│  conversations"│ │  Keycloak is the org's SSO / identity...   ││
│  list REMOVED  │ │  ▸ Sources (2)      ▸ How it decided       ││
│  — ADR-3 §4)   │ └───────────────────────────────────────────┘│
│                │ …more posts…                                 │
│                │ ┌───────────────────────────────────────────┐│
│                │ │ [compose — sticky bottom]         [Ask]    ││
│                │ └───────────────────────────────────────────┘│
└───────────────┴───────────────────────────────────────────────┘
```

### Data flow (no kernel change)

- **Initial load** (`GET /`): render the last `N=20` turns from `convlog.recent(viewer, 20)`
  as thread posts, oldest first (reverse the existing newest-first order), inside `.thread`.
  Empty state: one assistant-style system post ("Ask the swarm anything below.").
- **New ask** (`POST /ask`, unchanged endpoint): htmx target changes from
  `hx-target="#answer" hx-swap="innerHTML"` to `hx-target=".thread" hx-swap="beforeend"`.
  The response renders BOTH the user-post and the assistant-post as one fragment (two
  `<article>`s), so one htmx round-trip appends the full pair. A small `hx-on::after-swap`
  scrolls `.thread` to `scrollHeight` (no new JS file needed — one inline attribute,
  matching the existing inline-htmx-attribute style already used elsewhere in these templates).
- **Reopening history** (`/conversation/{id}`): today this replaces `#answer` with one
  post; with no more `#answer` slot, retarget it to scroll-and-highlight that post's
  position within the already-loaded thread, OR (simpler, since all recent turns are
  already inline) just drop the sidebar-triggered reopen entirely — the turn is already
  visible in the thread once loaded. Decide at build time whichever is less code; both are
  reversible template-only choices, not worth a design gate.

### Post partials

Split `_post.html` into:
- `_thread_user_post.html` — `.thread-post.user`: label "you" + timestamp + the raw
  question text (autoescaped, verbatim — no status/trace, users don't get graded).
- `_thread_assistant_post.html` — `.thread-post.assistant`: label "swarm" + status badge +
  confidence + the answer body, then two **native `<details>` disclosures** (no new JS):
  - `<details><summary>Sources ({{ citations|length }})</summary>` wrapping today's
    citation chip list (unchanged chip markup, just relocated inside the disclosure).
  - `<details><summary>How it decided</summary>` wrapping an htmx-loaded slot for
    `/deliberation/{{ ask_ref }}` — **only rendered when `ask_ref` is present** (unchanged
    rule), triggered via `hx-trigger="toggle"` on the `<details>` (loads on first expand,
    not on page load — keeps the thread fast for turns nobody inspects).

### CSS (new classes, additive — nothing existing removed except `.answer-area`/`#answer`)

- `.thread { display: flex; flex-direction: column; gap: 1rem; overflow-y: auto; }` —
  height bounded to the viewport minus topbar+composer (`calc(100vh - …)`), so it scrolls
  independently and the composer stays put — this is the one genuinely new layout idea
  epic 1 introduces (everything else reuses existing Basecoat + app.css primitives).
- `.thread-post` — a `.card` variant; `.thread-post.user` right-or-left aligned + a muted
  tint (bg `--secondary`) to read as "the asker" at a glance without reintroducing chat
  bubbles (still a full-width post-card, just visually distinct from an assistant post).
- `.compose` moves from top-of-main to `position: sticky; bottom: 0;` at the foot of
  `.thread`'s container — reuses the existing `.compose` class, just repositioned.
- Disclosures: style native `<details>/<summary>` to look like a small outline button
  (matching `.btn[data-variant=outline][data-size=sm]`'s existing look) rather than the
  browser default triangle — a few lines of `summary { ... }` CSS, no component library.

### Verification (epic 1's own gate, before deciding on epic 2)

- `pytest`/`ruff`/`ty`/`ruff format` green (existing gates, hive-check skill).
- Live screenshots (1440px + 720px): home with a populated thread, an assistant post with
  both disclosures expanded, the sticky composer at various scroll positions. Self-reviewed
  (operator explicitly delegated this judgment call) — not a council item (pure visual
  taste, reversible, zero data/security surface).
- A light code-review pass (codex or gemini) on the template/CSS diff — habit carried over
  from the rest of this campaign, not because this specific change is risky.

## Epic 2 — conversation-context memory (DECIDED, council-reviewed — ready to build)

**Problem**: `AskRequest` (swarm proto) has no history/context field — every `Ask` is
answered from zero, so a follow-up like "and its dependencies?" cannot resolve "its".

**The complication that changed this design** (found re-reading the ACTUAL current
pipeline before asking the council — item 3/ADR-17 world-map tier-gate shipped since
this section was first sketched): `synthesize/5` now tries `try_structured_gate/5`
(serve a structured answer straight from graph structure, bypassing the LLM entirely)
BEFORE ever reaching `deliberate/5` (the consilium). That gate needs candidate ENTITY
KEYS extracted from the query TEXT (`Procedure.candidates/3`, `hit_keys/1`) to even
attempt a serve — a bare pronoun produces no usable key, so folding "context" only into
the consilium prompt (the original plan) would make every context-dependent follow-up
miss the fast path and fall through to the expensive consilium, or fail outright.

**Council (codex + gemini, both grounded in the real pipeline, not guessing):**
- **Codex** recommended a kernel-side query-rewrite step before `Gate.route` — resolve
  referents into the query text itself via an LLM, cost-gated by a cheap
  "looks context-dependent" heuristic.
- **Gemini** (materially different, not a rubber-stamp) argued a *text* rewrite is the
  wrong shape for a *structured-key* problem — an LLM rewrite risks fidelity loss
  (mangling the exact key the fast path needs) and heuristic misses. Proposed instead:
  thread the **entity keys themselves** through the wire contract as `active_keys`, so
  the structured gate does `Extracted_Keys(query) UNION active_keys` — no LLM
  round-trip, no fidelity risk, zero added latency on the fast path.

**Decided shape (synthesis — the strongest part of each, neither's biggest risk):**
confirmed practical by reading `core.ex`'s `cite/1` — `Citation.ref` for a non-claim
citation IS ALREADY the graph node's `key` (`%{source: hit.type, ref: hit.key, ...}`),
so the channel can compute `active_keys` for the NEXT ask from the PREVIOUS
`AskResponse.citations` with **zero new kernel extraction work**:

1. `AskRequest` gains **two optional fields**: `repeated string active_keys` (entity
   keys from the prior turn's citations, channel-supplied) and `string
   conversation_id` (owner-enforced like `GetConversation`, used only on escalation).
2. `try_structured_gate/5`: `candidate_keys = Enum.uniq(Procedure.candidates(...) ++
   hit_keys(hits) ++ active_keys)` — one line, no change to `Procedure.candidates`,
   `Aggregation.entity_profile`, or any key-extraction internals. **No LLM rewrite
   anywhere in this path.**
3. `deliberate/5` (the escalate path only, where an LLM is being paid for anyway — the
   marginal cost of a little more prompt is near-free): when `conversation_id` is
   present, fetch bounded recent `message` rows (reusing the ADR-16 `Conversations`
   owner-enforcement exactly) and fold them into the `grounding` already threaded into
   `Consilium.deliberate`.
4. **No-leak**: `active_keys` being client-supplied is safe by construction — the
   structured gate already validates any candidate key against real graph structure AND
   the caller's scopes before ever serving from it (existing evidence-closed logic); a
   bogus or foreign-scoped key supplied via `active_keys` simply fails to match, exactly
   as harmless as a wrong guess baked into the query text. No new no-leak surface —
   `conversation_id` reuses the identical owner-enforcement predicate already
   ship-gate-tested for `GetConversation`.

This is DECIDED, not a sketch — build epic 2 from this section directly.

### Built + live-verified — 4 real gaps found and fixed (not caught by unit tests alone)

The design above was implemented as written, and the kernel-side unit tests (injected
retriever/entail_fun, synthetic candidate keys) all passed — but a live curl-driven
walkthrough against real staging data (`who manages Keycloak?` → `who manages it?`)
surfaced 4 real defects the synthetic tests couldn't see, each fixed + covered:

1. **Structured-gate citations were opaque, not the served key.** `structured_answer/1`
   wrapped only the gate's audit labels (`"corroboration:1"`) as citations — the design's
   "just re-read the previous `AskResponse.citations`" plan silently had nothing usable
   to read after a structured-served turn. Fixed: `Gate.Answer` gained a `:key` field,
   folded into citations alongside the opaque labels.
2. **Neighborhood domains (`:who`, `:network` — the only domains serve-enabled in
   staging) never saw `active_keys` at all.** The generic `candidate_keys` union (fix 1
   above feeds this) only reaches `Procedure.candidates`/entity_profile; each
   neighborhood domain sources its OWN candidates from `dom.candidates_fun.(query,
   scopes)`, a wholly separate opt. Fixed: `active_keys(opts)` is now unioned into every
   domain's own `<key>_keys` opt in `try_structured_gate/5`.
3. **The echoed key was the DISPLAY label, not the raw graph key.** `Validated.name`
   (used for both rendering AND, after fix 1, the citation) is `dom.subject_fun.(key)` —
   a one-way transform (a person resolves to their `cn`; a team/service key to its bare
   tail). `dom.neighborhood_fun` needs the real key ("who:service:keycloak", not
   "keycloak") and can never recover a person's key from their name. Fixed: threaded the
   raw key separately end to end (`Descriptor.neighborhood_key` → `Validated.key` →
   `Answer.key`), distinct from the display `name`/`subject`.
4. **web_channel never reused a kernel conversation across turns.** Every `/ask` passed
   `conversation_id=""` to both `Core.Ask` and its own dual-write — every turn silently
   created a throwaway conversation, so step 3's `prepend_history` had no real history to
   ever fold in, for ANY query. Fixed: the id `LogConversation` returns is stashed in the
   session and threaded into both the next Ask call and its own dual-write.

With all 4 fixed, the live walkthrough resolves end to end: `who manages Keycloak?`
serves structured (~3.5s, tier=structured); the bare pronoun follow-up `who manages
it?` correctly fails the Stage-2 semantic-entailment veto (it cannot confirm "it" =
Keycloak from grounding text alone — **this is Stage 2 working as designed**, not a
bug: ADR-17's veto is deliberately conservative against false-serves) and falls through
to the consilium, which — thanks to fix 4 — now has the real prior turn in its grounding
and answers correctly: *"Keycloak is managed by team dsi."* (tier=escalate,
confidence=0.59, ~61s). The fast path and the correct-but-slower fallback both work;
which one fires depends on how confidently Stage 2 can verify entity identity from text
alone — a property of ADR-17's calibration, not of this epic.

## Epic 3 — integrated threaded chat (SHIPPED — live-verified)

Ties 1+2 together: `/ask` continues one real kernel `conversation` per user; passes
`conversation_id` into `Ask` so epic 2's memory activates for real, not just in a
council-reviewed design.

**Turned out to already be built.** Epic 2's live-verify fix #4 (session-persisted
`conversation_id`, hive `efb064e`) *was* epic 3's steps 1+2 — reusing one kernel
conversation across turns and threading it into `Ask` is the same code either way.
Nothing left to build; what remained was deciding the read-path question and verifying
the end-to-end integration for real.

**Read-path decision: stay dual-sourced.** `convlog` (per-viewer, local) remains the
render source — it carries the trace fields (tier/confidence/citations/duration) the
kernel `message` model doesn't have; the kernel `conversation`/`message` model remains
the memory + privacy/ownership substrate (owner-enforced, dual-written best-effort).
Rejected moving rendering to kernel `message` or growing it trace fields: no concrete
pain point forces it — the dual-write already satisfies ADR-16's privacy invariant, and
growing the kernel schema to also carry render-trace data would be schema churn for a
problem that doesn't exist yet. Revisit only if the two sources are ever observed to
diverge in a way a user would notice (the dual-write is explicitly best-effort — "must
never break /ask" — so a lost kernel write is an accepted, pre-existing risk, not new).

**Live-verified** (staging, real data, `groot` session, screenshot):
`who manages Keycloak?` (tier=structured, 3.5s) → `who manages it?` (tier=escalate,
61.1s, confidence 0.59) → *"Keycloak is managed by team dsi."` — rendered as two
Discourse-style post pairs in one continuous thread, each with working `Sources`/`How it
decided` disclosures. One browser session, one real kernel conversation, memory
resolving a genuine pronoun follow-up, visual design intact. No-leak: re-verified at the
kernel unit level (`core_conversation_context_test.exs` — non-owning viewer gets no
history, a malformed viewer never crashes) rather than a fresh live adversarial account
— creating a new staging account fell outside the campaign's standing delegation.

## Correction (2026-07-08) — post objects, not a chat ribbon (SHIPPED)

Operator review rejected epic 1/3's LAYOUT (the memory mechanics of epic 2 survive
unchanged): rendering all history as one continuous thread with a bottom composer is
a messenger, not what the Mastodon/Discourse/Tumblr references meant. The corrected —
and now shipped — model (`feat/post-objects-rework`):

- **Each ask = a discrete POST OBJECT** (`_post_object.html`): a bounded card — the
  question as the post title, the answer inside the same object, then the post's OWN
  reply thread and a collapsed "follow up" form. Posts are future attachment points
  (images, audio…) — the object boundary is the point.
- **Follow-ups are replies under their post** (`_post_reply.html`, indented,
  forum-order): the reply form carries the post's `thread_id`; `/ask` with a
  `thread_id` continues THAT post's kernel conversation (`conversation_id`) and
  echoes the thread's last-turn citation keys as `active_keys`. A bare ask is a new
  topic: no carried context at all. Memory is **per-post**, not per-session — the
  session-global conversation (efb064e) is removed. The thread lookup is
  viewer-scoped: a foreign/bogus `thread_id` is an honest "gone", never context.
- **History is the sidebar again** ("Recent conversations", roots only — replies
  never appear as standalone history); clicking one loads that post + its thread
  into the feed (`/conversation/{id}`; a reply id resolves to its root). The home
  feed is never a wall of past history.
- **Compose is back on top**; new posts PREPEND to the feed.
- convlog: `thread_id` (reply → root row id; NULL = root, so all pre-rework rows are
  correctly flat roots) + `kernel_conv_id` (the kernel conversation backing the
  thread, set when the dual-write first succeeds — also backfills threads whose root
  predates signing).

Live-verified (staging, real data): post "who manages Keycloak?" → structured 3.3s;
reply "who manages it?" under it → escalate 44.1s → "Keycloak is managed by team
dsi." (thread memory); the SAME pronoun question as a fresh post did NOT inherit the
Keycloak context (per-post isolation confirmed). Kernel untouched throughout.
