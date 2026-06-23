---
date: 2026-06-23
status: Current
owner: hive
---

# Hive Operations

How to run the Swarm stack as a deployed instance. (Kernel internals live in
`../../swarm/`; shared architecture in `../../docs/`.)

## Topology

The whole instance runs in Docker, orchestrated from this repo's
`docker-compose.yml`:

| Service | Role | Scale |
| --- | --- | --- |
| `postgres` | pgvector store (included from `../swarm/dev/`) | singleton (state) |
| `ollama` | model runtime, **GPU** | singleton (one GPU) |
| `ml` | Python embed/generate gRPC service | **horizontal** (`deploy.replicas`) |
| `kernel` | Elixir/OTP control-plane (Core API :50061) | singleton (cluster later) |
| `migrate` | one-shot `Swarm.Release.migrate()`, then exits | — |

Boot order is enforced by `depends_on`: postgres healthy → migrate completes →
kernel starts; `ml` waits on `ollama` healthy. Each long-lived service runs with
`init: true` so the BEAM's `epmd` (and any child) is reaped — no zombies.

## Prerequisites

- Docker (with buildx + compose v2) and the **NVIDIA Container Toolkit**
  (`nvidia` runtime) for GPU passthrough to `ollama`.
- Ollama models on disk, bind-mounted read-only (not re-downloaded):
  `OLLAMA_MODELS_DIR` (default `/usr/share/ollama/.ollama/models`).
- Copy `.env.example` → `.env` and adjust. Key vars: DB creds, `OLLAMA_MODELS_DIR`,
  `HUB_REGISTRY` / `DHI_REGISTRY` (registry tier), `SWARM_CORE_API_PORT`.

## Run

```bash
docker compose up -d            # build (first time) + start the full stack
docker compose ps               # all healthy?
docker compose logs -f kernel   # follow

# end-to-end smoke (kernel → ml → ollama → 1024-d vector):
docker compose exec -T kernel /app/bin/swarm rpc \
  'Swarm.ML.Embeddings.embed(["hello"]) |> elem(1) |> Map.get(:dim) |> IO.inspect'
```

## Registry tiers

Images resolve by intent (digests are content-addressed, so the same pin works on
any mirror):

1. **local** `localhost:5000` — fully offline; the portable artifact.
2. **Smile public** `dockerhub.smile.fr` / `dhi.smile.fr` — defaults, safe to
   commit, flap-resistant.
3. **upstream** (Docker Hub / DHI) — implicit.

Switch tier with `HUB_REGISTRY` / `DHI_REGISTRY` (+ `UV_REGISTRY` for builds).

## Offline (air-gapped)

While online, mirror everything into the local registry once:

```bash
./scripts/mirror-to-local-registry.sh     # app + runtime images + build bases
```

Then run with no internet — images from `localhost:5000`, models from disk:

```bash
docker compose -f docker-compose.yml -f docker-compose.offline.yml up -d
```

Offline **build** (rebuild from local bases) also works:

```bash
HUB_REGISTRY=localhost:5000 DHI_REGISTRY=localhost:5000 UV_REGISTRY=localhost:5000 \
  docker compose build
```

Portable artifacts to carry to an air-gapped host: the `swarm_registry_data`
volume (all images) + the models directory.

## Scaling & HA

`ml` is the horizontal pillar — set `deploy.replicas`. Compose DNS round-robins
`ml`; the kernel reconnects per call and retries once on a transient failure, so
losing a replica is transparent. `ollama` (GPU) and `postgres` (state) stay
singletons; kernel clustering (BEAM + leader election) is future work.

## Security scanning

The intranet `docker-scanner` bundles trivy + hadolint + dockle (amd64; enable
qemu via `tonistiigi/binfmt --install amd64` on this arm64 host). trivy + hadolint
run under emulation; dockle needs an arm64-native binary.

## Troubleshooting

- **ML can't reach Ollama** → Ollama is a compose service (`ollama:11434`), not the
  host; don't set `OLLAMA_BASE_URL` to `localhost`.
- **GPU not used** → check the `nvidia` runtime and `nvidia-smi` inside `ollama`.
- **Out of VRAM with many models** → context length dominates KV cache; cap
  `num_ctx` or raise/lower `OLLAMA_MAX_LOADED_MODELS` (see swarm ADR-1).
