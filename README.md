# Swarm Hive

Private instance/deployment repo for a local Swarm setup.

This repo sits next to the public `swarm/` repo:

```text
swarm/
  swarm/      public kernel/control-plane repo
  hive/       private instance repo
```

Run from this directory:

```bash
docker compose up -d
```

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
