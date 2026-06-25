---
date: 2026-06-25
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

## Connectors (ingest sources)

Two real connectors live in `plugins/`, implementing the kernel's `fetch/2` contract
(swarm ADR-5) and emitting the `swarm_markdown_v1` body profile (structure preserved —
headings, tables, code):

| Plugin | Source | Auth | Notes |
| --- | --- | --- | --- |
| `mediawiki` | intranet MediaWiki | BotPassword (degrades to anon) | allpages + `continue` pagination; wikitext→md; redirect resolution |
| `confluence` | intranet Confluence | HTTP Basic | CQL search; opaque `_links.next` cursor; storage-XHTML→md; `group` scope |

Both are kernel-driven for completeness (`truncated?` on a real ceiling, never silent) and
carry a `:since` delta watermark for incremental top-up. **Loading:** currently auto-loaded
**in-process** by `Swarm.Plugins` (the dev-adapter mode, ports.md / ADR-11); the
out-of-process plugin ABI + manifest loader is future work (the `docker-compose.yml`
connector services are a commented skeleton until then).

**Config + secrets.** Each connector reads its base URL + credentials from env; the required
**names** are listed in `secrets.env.example` (`CONFLUENCE_URL/USER/TOKEN`,
`WIKI_URL/USER/USER_TOKEN`). Real values live only in `secrets.env`, **never committed**,
never in a public repo. Scope is set per connector (intranet sources default to `group`, so
they can never surface as `public`).

## Cognitive turn-on (shadow) — operator runbook

How to test the cognitive layer (reward-gated enrichment → entity-resolution → origin
accounting → relaxation) end-to-end on a **clone of the real corpus**, measure whether it
**improves answers** (gate 7), and calibrate — without risking production.

**The guard (non-negotiable).** The loop *mutates* state, so it runs **only on an isolated
persistent shadow DB**, never `swarm_dev` (conditional-prod). The harness asserts
`current_database()` and refuses `swarm_dev` or any env↔connected mismatch — so always set
`SWARM_DB_NAME` explicitly (there is no safe default in `MIX_ENV=dev`). Snapshot before, wipe/roll
back after; promotion to prod is a reviewed go/no-go, never automatic.

**Prereqs:** `postgres`, `ollama`+GPU (qwen3:14b), and `ml` gRPC (bge-m3) up; connector creds
loaded (`set -a; . hive/secrets.env; set +a`). All commands run from **`swarm/kernel/`** with
`MIX_ENV=dev SWARM_ML_ADDRESS=<ml host:port>` (e.g. `172.19.0.5:50051`).

**The one thing only you can produce — `qa.json`** (gate 7 lives or dies on it): real questions +
the node keys a correct answer should cite. External, **never committed** (keys are content).

```json
[{"q": "a real user question", "gold": ["node_key_of_the_right_page", "..."]}]
```

> **Do a small smoke FIRST, not the multi-day run.** One cycle, a small slice (low `*_MAXPAGES`),
> `CYCLES=1`, ~5 `qa.json` questions — validate *your* end-to-end flow (ingest → qa → loop → lift)
> in minutes. Then scale. (Operator-analog of CTC-5.)

### Steps

1. **Ingest the real corpus into the shadow** (Confluence + MediaWiki via the ADR-5 Sync; scale the
   `*_MAXPAGES` tunables up from the smoke):

   ```bash
   SWARM_DB_NAME=swarm_shadow SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev \
   CONF_MAXPAGES=… WIKI_MAXPAGES=… mise exec -- mix run --no-start \
     -r ../../hive/plugins/confluence_connector/confluence_connector.ex \
     -r ../../hive/plugins/mediawiki_connector/mediawiki_connector.ex \
     ../../hive/scripts/conn_2source_slice.exs
   ```

2. **Snapshot** the shadow (rollback insurance):

   ```bash
   docker exec hive-postgres-1 pg_dump -U swarm swarm_shadow > shadow-seed.sql
   ```

3. **Control run** — proves the measure/read path is non-mutating and the DB guard passes:

   ```bash
   LOOP_MODE=control SWARM_DB_NAME=swarm_shadow SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev \
     mise exec -- mix run --no-start ../../hive/scripts/cognitive_loop.exs
   ```

4. **Pre-loop answerability baseline** (capture BEFORE the hot run; needs `qa.json`):

   ```bash
   QUERY_SET=/abs/qa.json SCOPES=group RECALL_K=10 SWARM_DB_NAME=swarm_shadow MIX_ENV=dev \
   SWARM_ML_ADDRESS=172.19.0.5:50051 \
     mise exec -- mix run --no-start ../../hive/scripts/answerability_lift.exs
   ```

5. **Apply the CTC-5 priors** in `swarm/kernel/config/config.exs` (directional — re-derive in step 7):
   - **#3 ER over-proposes:** `config :swarm, :entity_resolution, vec_threshold:` `0.85` → **~0.93**.
   - **#4 reward gate non-selective:** `config :swarm, :enrichment, priority: [threshold:` `0.35` →
     **your p50** `]`. The default gates nothing on a fresh corpus → enrichment runs on *everything*
     at ~120 s/source. Do **not** copy the public 0.89; derive yours in step 7.

6. **Hot run** (`MAX_PER_PASS` = enrichment budget per pass; `CYCLES` = enrich→ER cadence rounds):

   ```bash
   LOOP_MODE=real CYCLES=4 ENRICH_ROUNDS=2 MAX_PER_PASS=20 \
   SWARM_DB_NAME=swarm_shadow SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev \
     mise exec -- mix run --no-start ../../hive/scripts/cognitive_loop.exs
   ```

   Each cycle prints aggregate **gauges** (no content): `entities`, `claims`, `merges`, `seen_max`,
   `top1` (single-super-node concentration), `frag` (un-merged key collisions), `rejected` (ER).
   The **circuit-breaker auto-halts + rolls back** on poisoning — concentration spike
   (`top1` jumps >0.3 above 0.5 with ≥8 claims), entity collapse (<0.7× prior), merge-rate spike, or
   `seen_max` runaway (>20). A clean run: `top1` flat/decreasing, `seen_max` low, `frag` → 0, no trip.

7. **Post-loop = the verdict.** Re-run the answerability harness (step 4) and **diff vs the baseline
   block — that lift is gate 7** (does cognition improve answers). Then:

   ```bash
   SWARM_DB_NAME=swarm_shadow MIX_ENV=dev \
     mise exec -- mix run --no-start ../../hive/scripts/calibrate.exs   # ER + reward thresholds from real logs
   ```

   Re-measure fragmentation → 0; **watch for multi-origin corroboration** (`seen_max` > 1 from
   distinct origins / exact-triple overlap) — if it appears, that is the trigger to promote the
   deferred lineage-aware clustering (workspace ADR-13). Fill the 10-gate go/no-go table in
   `board/research/cognitive-turn-on-calibration.md` with real numbers and convene a ≥2-family
   council before any promotion toward prod.

**Rollback.** The breaker rolls back automatically; to reset manually, restore the snapshot
(`docker exec -i hive-postgres-1 psql -U swarm swarm_shadow < shadow-seed.sql`) or delete just the
cognitive layer (entity nodes + `claim` edges + `enrichment_watermark` + `entity_resolution_audit`).
The ingested corpus is preserved; no enriched state should persist past a no-go.

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
