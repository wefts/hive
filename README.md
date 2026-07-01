# Swarm Hive

Private instance/deployment repo for a local Swarm setup.

This repo sits next to the public `swarm/` repo:

```text
swarm/
  swarm/      public kernel/control-plane repo
  hive/       private instance repo
```

Run from this directory (ADR-0015 — `SWARM_ENV` required, `test`/`staging`/`prod`):

```bash
SWARM_ENV=staging scripts/compose up -d
```

Full operational guide (topology, GPU/Ollama, registry tiers, offline, HA):
[`docs/operations.md`](docs/operations.md).

Plugin directories use:

```text
<domain>_<kind>
```

Examples:

```text
plugins/
  confluence_connector/
  k8s_tool/
```

Valid `kind` values for now: `connector`, `tool`, `worker`, `channel`,
`model`, `skill`.
