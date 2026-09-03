#!/usr/bin/env bash
# Run the ADR-21 MediaWiki connector image path against a disposable target DB.
#
# This is the compose-side scheduled unit for slice 1: start the MediaWiki
# connector sidecar, run the kernel ingest one-shot against it, then stop the
# sidecar. Confluence remains on the old loader until the MediaWiki shape is
# proven by the comparison run.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_db=""
allow_stage_db=0

usage() {
  cat <<'USAGE'
Usage:
  SWARM_ENV=staging scripts/mediawiki_connector_ingest.sh --target-db swarm_name

Options:
  --target-db DB       Target DB for the ingest run. Must start with swarm_.
  --allow-stage-db     Allow the SWARM_ENV-derived DB when no --target-db is set.
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
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
      echo "mediawiki_connector_ingest: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

guard_db_name() {
  local name="$1"
  case "$name" in
    ''|*[!a-z0-9_]*)
      echo "mediawiki_connector_ingest: DB must match [a-z0-9_]+, got '$name'" >&2
      exit 2
      ;;
  esac
  case "$name" in
    swarm_*) ;;
    *)
      echo "mediawiki_connector_ingest: DB must start with swarm_, got '$name'" >&2
      exit 2
      ;;
  esac
}

if [ -n "$target_db" ]; then
  guard_db_name "$target_db"
elif [ "$allow_stage_db" -ne 1 ]; then
  echo "mediawiki_connector_ingest: --target-db is required unless --allow-stage-db is set" >&2
  usage >&2
  exit 2
fi

cleanup() {
  SWARM_ENV="${SWARM_ENV:-staging}" "$here/scripts/compose" --profile jobs stop mediawiki_connector >/dev/null || true
}
trap cleanup EXIT

SWARM_ENV="${SWARM_ENV:-staging}" "$here/scripts/compose" --profile jobs up -d --no-recreate mediawiki_connector

if [ -n "$target_db" ]; then
  SWARM_ENV="${SWARM_ENV:-staging}" SWARM_DB_NAME="$target_db" \
    "$here/scripts/compose" --profile jobs run --rm --no-deps mediawiki_ingest
else
  SWARM_ENV="${SWARM_ENV:-staging}" "$here/scripts/compose" --profile jobs run --rm --no-deps mediawiki_ingest
fi
