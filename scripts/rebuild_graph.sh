#!/usr/bin/env bash
# Rebuild a disposable graph database from authoritative sources.
#
# This is STEP 1 of board/doing/disposable-graph.md only: a guarded, resumable
# orchestration entrypoint. It snapshots the reference DB, prepares a NAMED target
# DB, copies only access/project/source registry rows needed for ADR-20 source
# scopes, then runs the existing corpus, IaC/network-map, and who refresh loaders
# against the target. It never targets swarm_staging for destructive operations.
#
# Example:
#   SWARM_ENV=staging scripts/rebuild_graph.sh \
#     --target-db swarm_graph_rebuild_20260901 \
#     --recreate-target
#
# Private outputs stay under hive/tmp/ (gitignored). Source endpoints, credentials,
# LDAP coordinates, and IaC clone URLs come from gitignored env/secrets/tmp files or
# the shell; this script commits none of them.
set -euo pipefail

export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace="$(cd "$here/.." && pwd)"
postgres_container="${POSTGRES_CONTAINER:-hive-postgres-1}"
kernel_container="${KERNEL:-hive-kernel-1}"
source_db="${SOURCE_DB:-swarm_staging}"
target_db=""
recreate_target=0
resume=1

usage() {
  cat <<'USAGE'
Usage:
  scripts/rebuild_graph.sh --target-db swarm_name [--recreate-target] [--source-db swarm_staging]

Builds a named disposable graph DB from sources:
  1. snapshot the source/reference DB to hive/tmp/snapshots/
  2. create or recreate the target DB, then run kernel migrations
  3. copy only control-plane registry rows from the reference DB
  4. run scripts/ingest_prod.exs for Confluence + MediaWiki with bge-m3 embeddings
  5. run scripts/netmap/netmap_refresh.sh for deterministic IaC/wiki topology
  6. run scripts/who/who_refresh.sh for directory overlays

Guards:
  - --target-db is required and must match swarm_[a-z0-9_]+
  - destructive target recreation requires --recreate-target
  - swarm_staging is refused as a target
  - no deploy, push, env flip, enrichment, or heavy model work is performed here
USAGE
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --target-db)
      target_db="${2:-}"
      shift 2
      ;;
    --source-db)
      source_db="${2:-}"
      shift 2
      ;;
    --recreate-target)
      recreate_target=1
      shift
      ;;
    --no-resume)
      resume=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "rebuild_graph: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

guard_db_name() {
  local label="$1"
  local name="$2"
  case "$name" in
    ''|*[!a-z0-9_]*)
      echo "rebuild_graph: $label must match [a-z0-9_]+, got '$name'" >&2
      exit 2
      ;;
  esac
  case "$name" in
    swarm_*) ;;
    *)
      echo "rebuild_graph: $label must start with swarm_, got '$name'" >&2
      exit 2
      ;;
  esac
}

[ -n "$target_db" ] || { echo "rebuild_graph: --target-db is required" >&2; usage >&2; exit 2; }
guard_db_name "target DB" "$target_db"
guard_db_name "source DB" "$source_db"

if [ "$target_db" = "swarm_staging" ]; then
  echo "rebuild_graph: refusing to use swarm_staging as the rebuild target" >&2
  exit 2
fi

if [ "$target_db" = "$source_db" ]; then
  echo "rebuild_graph: target DB must differ from source/reference DB" >&2
  exit 2
fi

run_dir="$here/tmp/rebuild_graph/$target_db"
snap_dir="$here/tmp/snapshots"
log_dir="$run_dir/logs"
state_dir="$run_dir/state"
stamp="$(date +%Y%m%d_%H%M%S)"
mkdir -p "$snap_dir" "$log_dir" "$state_dir"
log="$log_dir/rebuild_${stamp}.log"

exec 9>"$run_dir/.lock"
flock -n 9 || { echo "rebuild_graph: another run holds $run_dir/.lock" >&2; exit 1; }

if [ "$recreate_target" -eq 1 ]; then
  rm -f "$state_dir"/*.done
fi

done_file() { printf '%s/%s.done' "$state_dir" "$1"; }
is_done() { [ "$resume" -eq 1 ] && [ -f "$(done_file "$1")" ]; }
mark_done() { date -Iseconds >"$(done_file "$1")"; }

run_step() {
  local name="$1"
  shift
  if is_done "$name"; then
    echo "-- skip $name (already done)"
    return 0
  fi
  echo "-- $name"
  "$@"
  mark_done "$name"
}

psql_db() {
  local db="$1"
  local sql="$2"
  docker exec "$postgres_container" psql -v ON_ERROR_STOP=1 -U swarm -d "$db" -c "$sql"
}

psql_scalar() {
  local db="$1"
  local sql="$2"
  docker exec "$postgres_container" psql -v ON_ERROR_STOP=1 -U swarm -d "$db" -tAc "$sql"
}

db_exists() {
  [ "$(psql_scalar postgres "SELECT 1 FROM pg_database WHERE datname = '$1'")" = "1" ]
}

snapshot_reference() {
  local out="$snap_dir/${source_db}_before_rebuild_${target_db}_${stamp}.dump"
  docker exec "$postgres_container" pg_dump -U swarm -Fc "$source_db" >"$out"
  echo "   snapshot -> $out"
}

prepare_target_db() {
  if db_exists "$target_db"; then
    if [ "$recreate_target" -ne 1 ]; then
      echo "   target exists; continuing without destructive recreation"
      return 0
    fi
    psql_db postgres "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$target_db' AND pid <> pg_backend_pid();"
    docker exec "$postgres_container" dropdb -U swarm --if-exists "$target_db"
  fi
  docker exec "$postgres_container" createdb -U swarm -O swarm --template=template0 "$target_db"
}

migrate_target_db() {
  SWARM_ENV="${SWARM_ENV:-staging}" \
    "$here/scripts/compose" run --rm --no-deps -e "SWARM_DB_NAME=$target_db" migrate
}

copy_registry_from_reference() {
  local dump="/tmp/rebuild_registry_${target_db}.dump"
  local tables=(
    app_user
    identity_link
    access_group
    user_group
    group_role
    sso_group_map
    project
    source
    project_membership
  )
  local table_args=()
  local table
  for table in "${tables[@]}"; do
    table_args+=(-t "$table")
  done

  psql_db "$target_db" \
    "TRUNCATE app_user, identity_link, access_group, user_group, group_role, sso_group_map, project, source, project_membership RESTART IDENTITY CASCADE;"
  docker exec "$postgres_container" pg_dump -U swarm -Fc --data-only "${table_args[@]}" -f "$dump" "$source_db"
  docker exec "$postgres_container" pg_restore -U swarm -d "$target_db" --data-only --disable-triggers --exit-on-error "$dump"
  docker exec "$postgres_container" rm -f "$dump"
}

ml_address() {
  if [ -n "${SWARM_ML_ADDRESS:-}" ]; then
    printf '%s\n' "$SWARM_ML_ADDRESS"
    return 0
  fi
  local ip
  ip="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' hive-ml-1)"
  [ -n "$ip" ] || { echo "rebuild_graph: could not resolve hive-ml-1 IP; set SWARM_ML_ADDRESS" >&2; exit 1; }
  printf '%s:50051\n' "$ip"
}

load_runtime_env() {
  # Source in the same broad order as scripts/compose so host-side Mix/plugin
  # code sees connector credentials and per-stage non-secret config.
  set -a
  [ -f "$here/env/base.env" ] && . "$here/env/base.env"
  [ -f "$here/env/${SWARM_ENV:-staging}.env" ] && . "$here/env/${SWARM_ENV:-staging}.env"
  [ -f "$here/secrets.env" ] && . "$here/secrets.env"
  set +a
}

run_corpus_ingest() {
  load_runtime_env
  local ml
  ml="$(ml_address)"
  (
    cd "$workspace/swarm/kernel"
    timeout "${INGEST_TIMEOUT:-4h}" env \
      SWARM_ENV="${SWARM_ENV:-staging}" \
      SWARM_DB_NAME="$target_db" \
      SWARM_ML_ADDRESS="$ml" \
      MIX_ENV=dev \
      CONF_MAXPAGES="${CONF_MAXPAGES:-1000000}" \
      WIKI_MAXPAGES="${WIKI_MAXPAGES:-1000000}" \
      EMBED_CONC="${EMBED_CONC:-4}" \
      mise exec -- mix run --no-start \
        -r ../../hive/plugins/confluence_connector/confluence_connector.ex \
        -r ../../hive/plugins/mediawiki_connector/mediawiki_connector.ex \
        ../../hive/scripts/ingest_prod.exs
  )
}

run_netmap_refresh() {
  SWARM_ENV="${SWARM_ENV:-staging}" SWARM_DB_NAME="$target_db" KERNEL="$kernel_container" \
    NETMAP_TMP="$here/tmp" LOG_DIR="$log_dir/netmap" \
    bash "$here/scripts/netmap/netmap_refresh.sh"
}

run_who_refresh() {
  SWARM_ENV="${SWARM_ENV:-staging}" SWARM_DB_NAME="$target_db" KERNEL="$kernel_container" \
    WHO_TMP="$here/tmp" LOG_DIR="$log_dir/who" \
    bash "$here/scripts/who/who_refresh.sh"
}

summarize_target() {
  psql_scalar "$target_db" \
    "SELECT 'nodes=' || (SELECT count(*) FROM node) || ' edges=' || (SELECT count(*) FROM edge) || ' content=' || (SELECT count(*) FROM content) || ' chunks=' || (SELECT count(*) FROM chunk) || ' sources=' || (SELECT count(*) FROM source);"
}

{
  echo "== disposable graph rebuild $stamp =="
  echo "   source_db=$source_db target_db=$target_db"
  echo "   log=$log"

  run_step snapshot_reference snapshot_reference
  run_step prepare_target_db prepare_target_db
  run_step migrate_target_db migrate_target_db
  run_step copy_registry_from_reference copy_registry_from_reference
  run_step corpus_ingest run_corpus_ingest
  run_step netmap_refresh run_netmap_refresh
  run_step who_refresh run_who_refresh

  echo "-- summary"
  summarize_target
  echo "== rebuild done =="
} 2>&1 | tee -a "$log"
