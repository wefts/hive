# Vendored static assets

These files are committed verbatim so the page **renders with no external network
call at runtime** (brief §5/§8) — the image is distroless, non-root and shell-less,
so nothing can be fetched on boot. They are vendored, not built: there is **no Node
or Tailwind toolchain** in this repo or in the image.

| File | Package | Version | License | sha256 |
|---|---|---|---|---|
| `basecoat.min.css` | [basecoat-css](https://basecoatui.com) (bundles tailwindcss v4.3.1) | 1.0.1 | MIT | `1bd2a6e1ce11fad0ac1266f5d85ce6b5affddd5fdb8f609d635fd7427ef1043d` |
| `htmx.min.js` | htmx | (see file header) | BSD-2 | — |
| `alpine.min.js` | Alpine.js | (see file header) | MIT | — |

## basecoat.min.css

The decided design system (brief: *Python · HTMX · Alpine.js · Tailwind · Basecoat*).
This is Basecoat's **self-contained CDN build** — Tailwind preflight is compiled in,
theme is exposed as CSS custom properties (`--background --foreground --primary
--border --radius --muted --card --ring …`), dark mode rides the `.dark` class on
`<html>`, and components ship as semantic classes (`.btn .card .input .badge .select
.table .sidebar .field .command …`). We override the palette + readability in
`../app.css` by setting those variables — **the one token layer**, no forking the bundle.

**Verified self-contained** (no runtime network): the bundle contains only inline
`data:` SVG URIs, the system font stack (no `@font-face`, no Google Fonts), and two
inert strings — a `tailwindcss.com` attribution comment and the `w3.org/2000/svg`
XML namespace. No `@import`, no remote `url()`.

### Refresh procedure

```sh
VER=1.0.1   # bump as needed; pin an exact version, never @latest
curl -sSfL "https://cdn.jsdelivr.net/npm/basecoat-css@${VER}/dist/basecoat.cdn.min.css" \
  -o src/web_channel/static/vendor/basecoat.min.css
sha256sum src/web_channel/static/vendor/basecoat.min.css   # record in the table above
# Re-verify no remote refs crept in:
grep -oE 'https?://[^ "]+' src/web_channel/static/vendor/basecoat.min.css | sort -u
#   expect only: https://tailwindcss.com  and  http://www.w3.org/2000/svg
```

Then update the version + sha256 row above and re-run the QA screenshots.
