#!/usr/bin/env bash
# Run the ADR-21 Proxmox connector image path for ONE site against a target DB.
#
# The compose-side scheduled unit for slice 3: start the Proxmox connector
# sidecar (it holds every configured site's credentials and refuses to start if
# a listed site is incomplete), run the kernel ingest one-shot for the named
# site, then stop the sidecar. One scheduled job per site: an unreachable
# datacenter fails ITS job and names itself; the others are untouched.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
site=""
target_db=""
allow_stage_db=0

usage() {
  cat <<'USAGE'
Usage:
  SWARM_ENV=staging scripts/proxmox_connector_ingest.sh --site casa --target-db swarm_name

Options:
  --site SITE          Site key listed in PROXMOX_SITES (lowercase). Required.
  --target-db DB       Target DB for the ingest run. Must start with swarm_.
  --allow-stage-db     Allow the SWARM_ENV-derived DB when no --target-db is set.
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

if [ -n "$target_db" ]; then
  guard_db_name "$target_db"
elif [ "$allow_stage_db" -ne 1 ]; then
  echo "proxmox_connector_ingest: --target-db is required unless --allow-stage-db is set" >&2
  usage >&2
  exit 2
fi

cleanup() {
  SWARM_ENV="${SWARM_ENV:-staging}" "$here/scripts/compose" --profile jobs stop proxmox_connector >/dev/null || true
}
trap cleanup EXIT

# Recreate ONLY the sidecar each run so a changed site URL, token, or CA path is picked up
# (a kept container keeps its first environment); --no-deps leaves the stack alone.
SWARM_ENV="${SWARM_ENV:-staging}" "$here/scripts/compose" --profile jobs up -d --no-deps --force-recreate proxmox_connector

if [ -n "$target_db" ]; then
  SWARM_ENV="${SWARM_ENV:-staging}" SWARM_DB_NAME="$target_db" PROXMOX_SITE="$site" \
    "$here/scripts/compose" --profile jobs run --rm --no-deps proxmox_ingest
else
  SWARM_ENV="${SWARM_ENV:-staging}" PROXMOX_SITE="$site" \
    "$here/scripts/compose" --profile jobs run --rm --no-deps proxmox_ingest
fi
