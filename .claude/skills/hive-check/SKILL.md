---
name: hive-check
description: >
  Run the hive deployment repo's gates before declaring work done. Use when the
  user says "check", "run gates", "lint", "verify", "is it clean", or after
  editing docker-compose.yml, scripts/, or the web_channel plugin in this repo.
  Never runs staging:up/deploy/db:backup/db:rename — those are deliberate
  operator actions, not part of "check".
---

# hive-check

Run the project gates and report pass/fail plainly. Never claim "clean" without
running them. Fail loud: surface the actual error output, do not summarize away.

## Preferred: the Taskfile

The gates are wired into `Taskfile.yml` (taskfile-pillar). From the repo root,
with the toolchain on PATH (`task`, `uv`) and a real `secrets.env` present
(`lint:compose` needs `KEYCLOAK_PUBLIC_URL`/`WEB_CHANNEL_PUBLIC_URL`/
`KEYCLOAK_PUBLIC_HOST` to resolve — see `secrets.env.example`):

```bash
task lint    # compose config (all 3 SWARM_ENV) + shell syntax + web_channel ruff/ty
task test    # web_channel pytest
task check   # lint + test
```

## Fallback: run the underlying tools directly

If `task` is unavailable, run the same gates by hand.

- **Compose config** (all three stages — requires `secrets.env`):

  ```bash
  for env in test staging prod; do SWARM_ENV=$env ./scripts/compose config >/dev/null && echo "$env: OK"; done
  ```

- **Shell scripts** (`bash -n`):

  ```bash
  for f in scripts/*.sh scripts/compose; do bash -n "$f"; done
  ```

- **web_channel** (`plugins/web_channel/`, uv-only — never `pip`/`python`):

  ```bash
  uv run ruff check .
  uv run ruff format --check .
  uv run ty check
  uv run pytest -q
  ```

## Rules

- Run every applicable gate, not just the first. Report each result.
- A non-zero exit is a failure — say so with the output, do not paper over it.
- `lint:compose` failing because a required var is unset (`KEYCLOAK_PUBLIC_URL`
  etc.) is a correct, honest failure — it means `secrets.env` isn't populated on
  this machine, not a bug in the compose file. Report it as such, don't "fix" it
  by adding a fake default.
- If a gate tool is missing, say which and how to get it; do not skip silently.
- **Never run `task staging:up` / `deploy` / `db:backup` / `db:rename` / `sync`
  as part of "check"** — those touch the live stack or a live DB and are
  deliberate, separately-authorized operator actions (see `hive/AGENTS.md`
  Boundaries and `board/done/environment-config.md`), never a side effect of
  verifying a change is clean.
