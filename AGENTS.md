# AGENTS.md — Hive Instance Repo

This is a **Hive** repo: a **public** deployment-instance repo for Swarm — it holds
deployment scaffold, not private data.

Read the workspace guide first: `../AGENTS.md`. Shared architecture, standards,
and current state live in `../docs/`; kernel implementation rules live in
`../swarm/`.

## What This Repo Owns

- Instance orchestration: `docker-compose.yml` (+ `docker-compose.offline.yml`),
  `Taskfile.yml` (taskfile-pillar — the canonical `task check`/`staging:up`/
  `deploy`/`db:backup`/`db:rename` flows; see Verification below).
- Layered, non-secret env config (ADR-0015): `env/base.env` + `env/<SWARM_ENV>.env`
  (`test`/`staging`/`prod`), all committed.
- Secret key templates: `secrets.env.example`.
- Committed plugin code under `plugins/` (public — see hive-publish-readiness-audit;
  private only insofar as it may be experimental or not-yet-generalized, per
  `docs/decisions/0011-hive-plugin-ownership.md`).
- Private (gitignored) data roots under `data/`.
- Hive-local helper scripts under `scripts/`, including `scripts/compose` — the
  layered-env entrypoint (`SWARM_ENV=staging scripts/compose up -d`).

This repo is **public**; it holds deployment scaffold, plugin code, and env *structure*.
It must not commit secrets, intranet hostnames/IPs, or private runtime data — those live
only in gitignored `secrets.env`, `data/`, and Docker volumes; intranet specifics are
parameterized to config, never hardcoded.

## Read First

- `README.md` — local Hive summary.
- `Taskfile.yml` — canonical operational flows (`task --list`).
- `docker-compose.yml` — current instance topology.
- `env/base.env`, `env/staging.env` — non-secret, per-stage config (ADR-0015).
- `secrets.env.example` — secret key names only, values empty.
- `../docs/architecture/ports.md` — plugin kinds, manifests, naming rule.
- `../docs/decisions/0011-hive-plugin-ownership.md` — why early plugins live here.
- `../docs/decisions/0015-environment-configuration-architecture.md` — env mechanism.
- `../docs/standards/guardrails.md` — hard boundaries.

## Boundaries

- Never write real secrets into committed files.
- Never edit or fabricate `secrets.env` through the agent.
- Never hand-edit `data/`; it is runtime/private state.
- `env/*.env` are committed and non-secret (ADR-0015) — real credentials stay in
  `secrets.env`; a machine-specific path (e.g. `OLLAMA_MODELS_DIR`) or a sandbox
  `SWARM_DB_NAME` override is a real shell export, never committed to `env/`.
- Plugin code may live here while it is private or experimental.
- Mature reusable plugins may move to standalone repos later; the kernel
  contract must not change when they do.
- Hive may depend on Swarm contracts; Swarm must not import Hive source.

## Plugins

Plugin naming, allowed port kinds, and manifest expectations are defined in
`../docs/architecture/ports.md`. Do not duplicate that list here.

Current placeholder plugin dirs:

```text
plugins/confluence_connector/
plugins/k8s_tool/
```

## Running The Hive

Run from this repo root — via the Taskfile (canonical) or the layered-env wrapper
directly; `SWARM_ENV` is REQUIRED (`test`/`staging`/`prod`), never guessed:

```bash
task staging:up                              # == SWARM_ENV=staging scripts/compose up -d
SWARM_ENV=staging scripts/compose up -d
SWARM_ENV=staging scripts/compose config
```

The full stack (postgres/pg_search + GPU ollama + ml + kernel) is documented in
[`docs/operations.md`](docs/operations.md) — topology, prerequisites, registry
tiers, offline run/build, scaling/HA, troubleshooting. `docker-compose.yml`
defines `postgres` directly (a from-source ParadeDB image, pinned to the local
registry) — it no longer includes `../swarm/dev/docker-compose.yml`, which is a
standalone local-dev/test helper only (`swarm_test`, plain pgvector), unrelated
to this deployment.

## Sync And Deployment

Remote sync is an operator action, never an agent default. The canonical
workspace sync boundary is `../docs/decisions/0012-operator-sync-boundary.md`.

Hive-local scripts exist for private-layer work, but do not run remote sync
unless the human explicitly asks for it.

## Verification

For Hive changes, prefer the `Taskfile.yml` (taskfile-pillar) — same gates, one command:

```bash
task check          # lint + test
task lint           # compose config (all 3 SWARM_ENV) + shell syntax + web_channel ruff/ty
task test           # web_channel pytest
```

Fallback — run the underlying checks directly:

```bash
SWARM_ENV=staging scripts/compose config
bash -n scripts/env.sh
bash -n scripts/deploy.sh
bash -n scripts/compose
```

If Docker or shell tooling is unavailable, report that honestly.

## Instruction Files

This file is the canonical agent guide for the `hive/` repo.
