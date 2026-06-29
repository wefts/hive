# ADR-2 (hive): web_channel design system — vendored Basecoat, one token layer, no build step

## Status

Accepted (council-reviewed 2026-06-29: codex gpt-5.5 + gemini-pro both
SOUND-WITH-CAVEATS; both caveats folded in — see `board/journal.md`).

## Context

The brief fixes the stack: *Python · HTMX · Alpine.js · Tailwind · Basecoat*
(`board/ideas/hive-chat-channel.md:13,97,330`). P0–P2 shipped on **bespoke
hand-written CSS** (~204 lines) to move fast — deliberate debt. Every new UI
surface (dashboard, evidence, deliberation views) pays a bespoke-spacing/colour
tax against that hand-CSS until the decided system is adopted. The surface is at
its smallest now (~475 lines of templates), so this is the cheapest moment to
converge to canon. ADR-1 stands: the channel is a thin, deterministic gRPC
renderer — this ADR only concerns *how the markup is styled*, nothing about data
flow or scope.

A hard runtime invariant (brief §5/§8): the page **renders with no external
network call** — the image is distroless, non-root, shell-less, so nothing can be
fetched on boot. `htmx.min.js` / `alpine.min.js` are already vendored as committed
static files with no build step.

## Decision

1. **Vendor Basecoat's self-contained CDN build** (`basecoat-css@1.0.1`'s
   `basecoat.cdn.min.css`, MIT; bundles tailwindcss v4.3.1, MIT) as a single
   committed static file `static/vendor/basecoat.min.css` — exactly like
   htmx/alpine. **No Node, no Tailwind build, no purge step** anywhere in the repo
   or image. The bundle carries the Tailwind preflight, theme CSS-variables, the
   `.dark` mode class, and component classes (`.btn .card .input .badge .select
   .table .sidebar .field .command …`).
2. **One token layer only.** `static/app.css` shrinks to: (a) override Basecoat's
   CSS variables to our dark palette, and (b) scale the readability foundation
   (fluid `clamp()` root font-size, 1.6 line-height — the operator's limited-eyesight
   need, `tmp/notes/readability.md`) plus the few layout primitives Basecoat has no
   component for (the 2-col grid, sticky sidebar). We never fork the bundle.
3. **No `basecoat.js`.** The interactive widgets it powers (dialog/dropdown/toast)
   aren't used; the ⌘K palette stays Alpine-driven over Basecoat's static `.command`
   styling. Keeps the JS surface at htmx + alpine.
4. **Templates use Basecoat semantic classes**, not bespoke CSS.
5. **Provenance is pinned and documented** (`static/vendor/VENDORING.md`): exact
   version, sha256, license, source URL, and a refresh-and-re-verify procedure. The
   bundle is verified free of remote `@import`/font/`url()` references, so the
   no-network invariant holds by inspection.

## Consequences

- Future UI features compose from a real design system instead of accreting
  bespoke CSS — the tax the architect flagged is paid down at the cheapest moment.
- The runtime stays offline and the toolchain stays Node-free; reproducibility is a
  pinned file + sha, not a build pipeline.
- Cost: a 217 KB CSS payload (unpurged) on a localhost operator tool — negligible
  for this surface, and re-evaluable later (a purge build) if it ever matters.
- Visual canon is now "Basecoat defaults + our token overrides"; the dark palette
  and readability live in one small file, swap-able centrally.

## Alternatives rejected

- **A real Tailwind purge build (~15–30 KB output).** Materially better payload,
  but reintroduces a Node/Tailwind toolchain we otherwise don't need, for a
  localhost tool where 217 KB is irrelevant — over-engineering at this scale
  (the architect's explicit caution). Revisit only if payload ever matters.
- **Vendoring per-component CSS files.** Needs `@apply`/Tailwind at build time to
  resolve — same toolchain cost, no benefit over the self-contained bundle.
- **Keep bespoke hand-CSS.** The debt being paid down; diverges from the fixed stack.
