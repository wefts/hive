# web_channel — Swarm operator web console

A `channel` plugin (`docs/architecture/ports.md`): the web sibling of `cli_channel`.
A thin **FastAPI + HTMX** server-side renderer that is a **gRPC client of the kernel
Core API** (`swarm.core.v1`, via `grpc.aio`). The kernel owns cognition and scope;
the channel only renders structured facts and accepts input. **It never reads the
graph DB** — every datum comes through a typed, scope-enforcing Core RPC (ADR-1).

Plan: `hive/docs/design/web_channel-plan.md` · ADR: `hive/docs/decisions/0001-web-channel-architecture.md`.

## Phase

**P0 (this cut):** a walking skeleton — one input box → one honest answer card
(status badge, confidence, citation chips), rendered **deterministically** (status
from the structured field, never inferred from prose; values verbatim + HTML-escaped).
Fixed operator viewer, `public` scope, **no auth** (auth is P1). Uses only `Ask`.

## Develop (local, no Docker)

```bash
cd hive/plugins/web_channel
uv sync
bash scripts/gen-proto.sh                 # generate _gen/ from proto/core.proto
SWARM_CORE_ADDR=127.0.0.1:50061 uv run uvicorn web_channel.main:app --reload --port 8080
# open http://127.0.0.1:8080
```

## Verify

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                              # render + escaping + contract (stub Core server)
```

## Layout

```text
proto/core.proto          vendored copy of the kernel contract (refreshed by gen-proto.sh)
scripts/gen-proto.sh      protoc → src/web_channel/_gen/ (gitignored)
src/web_channel/
  core_client.py          grpc.aio client of Core.Ask
  render.py               deterministic status/confidence mapping (mirrors swarm/cli)
  main.py                 FastAPI app: GET / (input), POST /ask (HTMX partial)
  templates/              Jinja2 (autoescape on): base, index, _answer partial
  static/                 vendored htmx + alpine + app.css (no external network call)
tests/                    unit render, escaping, and a grpc.aio contract test
```

> **Styling note (P0):** ships a small self-contained `app.css` (dark-mode tokens,
> card/badge/chip) and vendored HTMX + Alpine so the page **renders with no external
> network call** (brief §5/§8). The full **Tailwind + Basecoat** design-system build
> is introduced in **P2** (the dashboard), where the token layer earns its place.
