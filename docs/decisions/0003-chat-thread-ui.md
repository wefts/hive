# ADR-3 (hive): chat thread UI — Discourse post-stream layout + Claude.ai-style per-message metadata

## Status

Accepted (2026-07-07) — **layout decision corrected 2026-07-08 by operator review.**

What shipped on 07-07 rendered the history as ONE continuous chat ribbon (all past
turns as a wall on home, composer stuck at the bottom). The operator rejected that:
the social/forum references (Mastodon, Discourse, Tumblr) all meant **discrete post
objects** — each question is a standalone bounded post, the answer inside it,
follow-ups as replies UNDER that post (a per-post thread), history back in the
sidebar, compose on top. Posts are future attachment points (images, audio — the
Tumblr idea); the object model is the point, a ribbon erases it. The corrected
shape shipped same day (`feat/post-objects-rework`, c91ab85): per-POST kernel
conversation (memory is thread-scoped, not session-scoped); the epic-2 kernel wire
contract (`active_keys` + `conversation_id`) fits unchanged.

Original arc: three gated epics — visual redesign first (this ADR), then
conversational memory, then integration. Council: none for epic 1 (pure UI/UX,
low blast-radius, reversible); epic 2's kernel wire-contract change got
codex+gemini council per the campaign's stakes rule. See
`hive/docs/design/chat-thread-ui.md` for the built shape, epic 2's 4 live-verify
findings/fixes, and the post-objects correction.

## Context

The current home view (`board/done/…microblog…`, 2026-06-29) is a **flat, one-shot
feed**: a compose box on top, a single `#answer` slot that is *replaced* on every
`/ask` (not appended to), and a "Recent conversations" sidebar of links that each
also replace the slot with one isolated past turn. Each Q&A is its own atomic
"toot" — deliberately modeled on Mastodon's single-post feel per the 2026-06-29
brief. That served its purpose (a fast, honest, no-framework upgrade over raw
text) but doesn't feel like a *conversation* — there's no sense of an ongoing
thread, and re-opening history means losing the current view.

The operator identified a better-fitting reference after living with it a while:
**Discourse's private-message (PM) pattern** — still "posts" (because the
underlying substrate is a forum topic), but rendered and used as a chat. Verified
directly (not assumed) via Discourse's own source/docs: a PM is literally a
`Topic`; each reply is a `post` object (`post_number`, `username`,
`avatar_template`, `created_at`, `cooked` HTML body, optional
`reply_to_post_number`), rendered by the *same* `post_stream`/`cloaked-collection`
component as a public topic — a virtualized, infinite-scrolling stream of
post-cards, not chat-bubbles. ([Discourse Chat UI Components — DeepWiki](https://deepwiki.com/discourse/discourse/9.2-chat-ui-components),
[Discourse GitHub](https://github.com/discourse/discourse))

Separately, the operator wants the **answer's metadata** (citations, the
panel-vs-judge deliberation) presented the way **claude.ai's web client** does:
collapsed-by-default disclosures next to an assistant turn ("Sources" pill,
expandable "thinking"/reasoning panel) rather than always-visible inline chips
and a raw button — we already have the *data* (citations, `ask_ref` →
Deliberation), just not that per-message collapsible affordance.

Swarm's kernel already has the substrate this maps onto almost exactly (ADR-16,
step 6b): a `conversation`/`message` model — `conversation(id, owner_id, title,
created_at)` / `message(id, conversation_id, role, body, author_user_id, ask_ref,
created_at)` — structurally the same shape as Discourse's `topic`/`post`, with
`role: user|assistant` replacing Discourse's always-human authorship. It exists
today for the ADR-16 privacy invariant (dual-written from `/ask`, step 6b.3) but
is **not yet the UI's source of truth** — the local `convlog` (richer: carries
tier/confidence/citations/duration, which kernel `message` doesn't) still is.

## Decision (this ADR covers epic 1 only — the visual layer)

1. **The home view becomes one continuous, scrollable thread**, not a
   single-slot-replace. On load, the user's recent turns (from `convlog`, newest
   at the bottom) populate the thread; the compose box moves to the **bottom**
   (sticky), matching both Discourse-PM-as-chat and claude.ai's layout — the
   thread scrolls above a fixed composer, not the other way round. A new `/ask`
   **appends** a post-pair to the bottom (`hx-swap="beforeend"` + scroll-to-bottom),
   never replaces the view.
2. **Two post shapes, not one generic card**: a compact user-post (query only, no
   trace) and a fuller assistant-post (status/confidence/answer, plus two
   **collapsed-by-default disclosures**: "Sources (N)" — today's citation chips,
   moved behind a toggle — and "How it decided" — today's `ask_ref` → Deliberation
   button, restyled as an inline expand rather than a separate fetch-and-hide
   button). Both post shapes reuse Basecoat components (`.card`, `.badge`,
   `<details>`/`.collapsible` — no new JS framework, htmx + a native `<details>`
   element covers the disclosure interaction with zero new JS).
3. **Single continuous thread per viewer, not multiple named topics (yet).**
   Discourse's real PM model supports many distinct topics; we deliberately start
   with ONE ongoing thread per user (simplest slice that delivers the "chat, not
   disconnected toots" feeling) and defer multi-topic navigation as a later
   decision if the operator wants it once this ships.
4. **The "Recent conversations" sidebar list is dropped for the home view** — it
   is now redundant with the thread itself (which already shows history inline).
   The "State of my memory" tile stays in the sidebar. (Kept in mind, not yet
   decided: if we ever add multi-topic threads, a topic switcher would replace
   this sidebar slot then — not now.)
5. **No kernel change in this epic.** The thread's data source stays `convlog`
   (it is the only store carrying the render-necessary trace fields). Epics 2/3
   revisit whether `message`/`conversation` should become the source of truth
   once conversational memory needs it kernel-side anyway.

## Consequences

- Home's read path changes from "one recent turn or the live answer" to "the last
  N turns, live-appended" — slightly more HTML per load, negligible at this scale
  (single-operator/small-cohort instance).
- `_post.html` splits into role-specific partials; `main.py`'s `/ask` response
  changes from a single post fragment to whatever the new append-target needs
  (still one HTMX response per ask — no new endpoint).
- Sets up epics 2 (kernel conversational memory) and 3 (kernel `message` becomes
  the thread's real source of truth, tying memory to what's rendered) without
  committing to either yet — this ADR is scoped to the visual layer only.

## Alternatives rejected

- **Chat-bubble UI (Slack/Discord/ChatGPT-classic style).** Rejected per the
  operator's explicit steer toward the Discourse "still posts" feel — bubbles
  drop the post-metadata (status/confidence/citations) that a research-answering
  system needs to show per-turn, whereas a post-card has room for it naturally.
- **Multi-topic threads now.** More correct long-term (mirrors Discourse's real
  PM model and the kernel's `conversation` being multi-instance already) but
  adds a topic-switcher UI this epic doesn't need to deliver the core feel;
  deferred to avoid scope creep in the "simple design first" pass.
- **Move the thread to kernel `message` as source-of-truth immediately.** Would
  require extending kernel `message` with confidence/citations/tier fields (a
  kernel schema change) before any visual payoff — backwards from the operator's
  requested order (simple design → verify → memory → integration).
