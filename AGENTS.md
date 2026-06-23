# AGENTS.md — Hive Instance Repo

This is a **Hive** repo: a private deployment instance for Swarm.

Read the workspace guide first: `../AGENTS.md`. Shared architecture, standards,
and current state live in `../docs/`; kernel implementation rules live in
`../swarm/`.

## What This Repo Owns

- Instance orchestration: `docker-compose.yml`.
- Non-secret env examples: `.env.example`.
- Secret key templates: `secrets.env.example`.
- Private/local plugins under `plugins/`.
- Private data roots under `data/`.
- Hive-local helper scripts under `scripts/`.

This repo may contain private integration code and local deployment choices. It
must not leak secrets or private runtime data into committed files.

## Read First

- `README.md` — local Hive summary.
- `docker-compose.yml` — current instance topology.
- `.env.example` — non-secret machine config.
- `secrets.env.example` — secret key names only, values empty.
- `../docs/architecture/ports.md` — plugin kinds, manifests, naming rule.
- `../docs/decisions/0011-hive-plugin-ownership.md` — why early plugins live here.
- `../docs/standards/guardrails.md` — hard boundaries.

## Boundaries

- Never write real secrets into committed files.
- Never edit or fabricate `secrets.env` through the agent.
- Never hand-edit `data/`; it is runtime/private state.
- Keep `.env` per machine and uncommitted.
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

Run from this repo root:

```bash
docker compose up -d
docker compose config
```

The full stack (postgres + GPU ollama + ml + kernel) is documented in
[`docs/operations.md`](docs/operations.md) — topology, prerequisites, registry
tiers, offline run/build, scaling/HA, troubleshooting. `docker-compose.yml`
includes the generic Swarm substrate from `../swarm/dev/docker-compose.yml`.

## Sync And Deployment

Remote sync is an operator action, never an agent default. The canonical
workspace sync boundary is `../docs/decisions/0012-operator-sync-boundary.md`.

Hive-local scripts exist for private-layer work, but do not run remote sync
unless the human explicitly asks for it.

## Verification

For Hive changes, prefer:

```bash
docker compose config
bash -n scripts/env.sh
bash -n scripts/deploy.sh
```

If Docker or shell tooling is unavailable, report that honestly.

## Instruction Files

This file is the canonical agent guide for the `hive/` repo.
