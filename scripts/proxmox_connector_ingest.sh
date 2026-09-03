#!/usr/bin/env bash
# Run the ADR-21 Proxmox connector image path for ONE site against a target DB.
#
# The compose-side scheduled unit for slice 3: start the Proxmox connector
# sidecar (it holds every configured site's credentials and refuses to start if
# a listed site is incomplete), run the kernel ingest one-shot for the named
# site, then stop the sidecar. One scheduled job per site: an unreachable
# datacenter fails ITS job and names itself; the others are untouched.
#
# Preflight — the target must be READY, and this script either makes it so or
# says exactly what is missing (a path that only works when you already know
# three undocumented steps is not a path):
#   1. the target DB exists (a missing --target-db is created EMPTY; for a copy
#      of a live graph run `task db:rename SRC=swarm_staging DST=<db>` first);
#   2. it is migrated with the SAME kernel image the job will run
#      (SWARM_KERNEL_VERSION), and that image must carry schema >= 13
#      (edge_validity) — an older image stops at 12 and the job cannot run;
#   3. a `proxmox` Source is registered (default project: PROXMOX_PROJECT,
#      "Operations"), or PROXMOX_SOURCE_ID names one.
# With --allow-stage-db (the SWARM_ENV-derived DB) nothing is created or
# migrated automatically — the checks run and fail with instructions instead:
# a stage DB is migrated by `task deploy SERVICE=kernel`, never by a job.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
site=""
target_db=""
allow_stage_db=0
required_schema=13
pg_container="${SWARM_PG_CONTAINER:-hive-postgres-1}"

usage() {
  cat <<'USAGE'
Usage:
  SWARM_ENV=staging scripts/proxmox_connector_ingest.sh --site casa --target-db swarm_name

Options:
  --site SITE          Site key listed in PROXMOX_SITES (lowercase). Required.
  --target-db DB       Target DB for the ingest run. Must start with swarm_.
                       Created empty and migrated if missing.
  --allow-stage-db     Allow the SWARM_ENV-derived DB when no --target-db is set
                       (checked, never created or migrated here).

Environment:
  SWARM_KERNEL_VERSION  kernel image tag used for BOTH migrate and the job
                        (must carry schema >= 13 — the edge_validity migration).
  PROXMOX_PROJECT       project that owns the auto-registered `proxmox` Source
                        (default: Operations). PROXMOX_SOURCE_ID overrides lookup.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --site)
      site="${2:-}"
      shift 2
      ;;
    --target-db)
      target_db="${2:-}"
      shift 2
      ;;
    --allow-stage-db)
      allow_stage_db=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "proxmox_connector_ingest: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "$site" in
  ''|*[!a-z0-9_-]*)
    echo "proxmox_connector_ingest: --site must match [a-z0-9_-]+, got '$site'" >&2
    exit 2
    ;;
esac

guard_db_name() {
  local name="$1"
  case "$name" in
    ''|*[!a-z0-9_]*)
      echo "proxmox_connector_ingest: DB must match [a-z0-9_]+, got '$name'" >&2
      exit 2
      ;;
  esac
  case "$name" in
    swarm_*) ;;
    *)
      echo "proxmox_connector_ingest: DB must start with swarm_, got '$name'" >&2
      exit 2
      ;;
  esac
}

SWARM_ENV="${SWARM_ENV:-staging}"
export SWARM_ENV

if [ -n "$target_db" ]; then
  guard_db_name "$target_db"
elif [ "$allow_stage_db" -eq 1 ]; then
  target_db="${SWARM_DB_NAME:-swarm_${SWARM_ENV}}"
  guard_db_name "$target_db"
else
  echo "proxmox_connector_ingest: --target-db is required unless --allow-stage-db is set" >&2
  usage >&2
  exit 2
fi

compose() { "$here/scripts/compose" "$@"; }
pg() { docker exec -i "$pg_container" psql -U swarm -d "$1" -Atqc "$2" 2>/dev/null; }
die() { echo "proxmox_connector_ingest: $*" >&2; exit 1; }

kernel_tag="${SWARM_KERNEL_VERSION:-<compose default, see docker-compose.yml>}"

# --- preflight 1: the DB exists -----------------------------------------------------------
if [ "$(pg postgres "SELECT 1 FROM pg_database WHERE datname = '$target_db'")" != "1" ]; then
  if [ "$allow_stage_db" -eq 1 ]; then
    die "stage DB '$target_db' does not exist; bring the stack up first (task staging:up)"
  fi
  echo "proxmox_connector_ingest: target '$target_db' does not exist — creating it EMPTY" >&2
  echo "  (for a copy of a live graph: task db:rename SRC=swarm_staging DST=$target_db, then re-run)" >&2
  docker exec -i "$pg_container" createdb -U swarm -O swarm --template=template0 "$target_db" \
    || die "could not create '$target_db'"
fi

# --- preflight 2: migrated by the SAME image the job runs -------------------------------------
schema="$(pg "$target_db" "SELECT version FROM graph_schema_meta WHERE id = 1" || true)"
if [ -z "$schema" ] || [ "$schema" -lt "$required_schema" ]; then
  if [ "$allow_stage_db" -eq 1 ]; then
    die "stage DB '$target_db' is at schema '${schema:-none}', the proxmox job needs >= $required_schema (edge_validity); migrate it with the normal deploy path: task deploy SERVICE=kernel using a kernel image that carries it"
  fi
  echo "proxmox_connector_ingest: '$target_db' at schema '${schema:-none}' — migrating with swarm-kernel:${kernel_tag}" >&2
  SWARM_DB_NAME="$target_db" compose run --rm --no-deps migrate >/dev/null \
    || die "migrate failed for '$target_db' (image swarm-kernel:${kernel_tag}); see the compose output above"
  schema="$(pg "$target_db" "SELECT version FROM graph_schema_meta WHERE id = 1" || true)"
  if [ -z "$schema" ] || [ "$schema" -lt "$required_schema" ]; then
    die "after migrate '$target_db' is at schema '${schema:-none}', the proxmox job needs >= $required_schema (edge_validity). The kernel image swarm-kernel:${kernel_tag} predates that migration — set SWARM_KERNEL_VERSION to an image built from swarm >= 56beea2 (the same variable drives the job) and re-run"
  fi
fi

# --- preflight 3: a `proxmox` Source is registered --------------------------------------------
if [ -z "${PROXMOX_SOURCE_ID:-}" ]; then
  sources="$(pg "$target_db" "SELECT count(*) FROM source WHERE kind = 'proxmox'")"
  case "$sources" in
    0)
      project="${PROXMOX_PROJECT:-Operations}"
      if [ "$allow_stage_db" -eq 1 ]; then
        die "stage DB '$target_db' has no Source of kind proxmox; register one under a Project (admin console or Swarm.Projects.add_source) or set PROXMOX_SOURCE_ID"
      fi
      if [ "$(pg "$target_db" "SELECT count(*) FROM project WHERE name = '$project'")" != "1" ]; then
        die "no project named '$project' in '$target_db' to own the proxmox Source; set PROXMOX_PROJECT to an existing project name (or create one) — an EMPTY DB has none, a copy of staging has Operations"
      fi
      echo "proxmox_connector_ingest: registering Source kind=proxmox under project '$project'" >&2
      SWARM_DB_NAME="$target_db" compose --profile jobs run --rm --no-deps proxmox_ingest eval "
        {:ok, _} = Application.ensure_all_started(:swarm)
        %{rows: [[pid]]} = Swarm.Repo.query!(\"SELECT id::text FROM project WHERE name = \$1\", [\"$project\"])
        {:ok, src} = Swarm.Projects.add_source(pid, %{kind: \"proxmox\", label: \"proxmox\"})
        IO.puts(\"proxmox_connector_ingest.source_registered id=#{src.id} scope=#{src.scope}\")
      " 2>&1 | grep "proxmox_connector_ingest.source_registered" || die "registering the proxmox Source failed"
      ;;
    1) ;;
    *)
      die "'$target_db' has $sources Sources of kind proxmox; set PROXMOX_SOURCE_ID to the one this site writes under"
      ;;
  esac
fi

# --- the job --------------------------------------------------------------------------------
cleanup() {
  compose --profile jobs stop proxmox_connector >/dev/null || true
}
trap cleanup EXIT

# Recreate ONLY the sidecar each run so a changed site URL, token, or CA path is picked up
# (a kept container keeps its first environment); --no-deps leaves the stack alone.
compose --profile jobs up -d --no-deps --force-recreate proxmox_connector

SWARM_DB_NAME="$target_db" PROXMOX_SITE="$site" \
  compose --profile jobs run --rm --no-deps proxmox_ingest
