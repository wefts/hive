#!/usr/bin/env bash
# Hive deploy/sync to Spark (no CI). PRIVATE layer (outside the public repo).
#
#   deploy.sh repo    [push|pull]   delegate to the public repo sync (../swarm <-> ~/Swarm/swarm)
#   deploy.sh plugins [push|pull]   ./plugins <-> ~/Swarm/hive/plugins
#   deploy.sh stack    push         hive compose/env examples -> ~/Swarm/hive
#   deploy.sh all      push|pull    repo + plugins + stack on push
#
# NEVER transferred (SECURITY_EXCLUDES): secrets.env, .env, data/, tmp/.
# `stack` ships only orchestration + *.example templates — real .env/secrets.env
# are created per machine and stay put.
set -euo pipefail

target="${1:-}"; dir="${2:-push}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$here/env.sh"

hive_remote="$(ssh "${SPARK}" "echo ${HIVE_REMOTE}")"
pl_remote="$(ssh "${SPARK}" "echo ${PLUGINS_REMOTE}")"

sync_plugins() {
  ssh "${SPARK}" "mkdir -p '${pl_remote}'"
  case "$1" in
    push)
      echo ">> plugins push (mirror) -> ${SPARK}:${pl_remote}/"
      rsync -az --delete "${BUILD_EXCLUDES[@]}" "${SECURITY_EXCLUDES[@]}" \
        "${HIVE_LOCAL}/plugins/" "${SPARK}:${pl_remote}/" ;;
    pull)
      echo ">> plugins pull (additive) <- ${SPARK}:${pl_remote}/"
      rsync -az "${BUILD_EXCLUDES[@]}" "${SECURITY_EXCLUDES[@]}" \
        "${SPARK}:${pl_remote}/" "${HIVE_LOCAL}/plugins/" ;;
    *) echo "plugins: push|pull" >&2; exit 2 ;;
  esac
}

sync_stack() {
  [ "$1" = push ] || { echo "stack: push only" >&2; exit 2; }
  ssh "${SPARK}" "mkdir -p '${hive_remote}'"
  echo ">> stack push -> ${SPARK}:${hive_remote}/ (orchestration + templates)"
  # Explicit file list + SECURITY_EXCLUDES backstop: real secrets/.env/data never go.
  rsync -az "${SECURITY_EXCLUDES[@]}" \
    "${HIVE_LOCAL}/docker-compose.yml" \
    "${HIVE_LOCAL}/.env.example" \
    "${HIVE_LOCAL}/secrets.env.example" \
    "${SPARK}:${hive_remote}/"
}

case "$target" in
  repo)    "${REPO_LOCAL}/scripts/sync.sh" "$dir" ;;
  plugins) sync_plugins "$dir" ;;
  stack)   sync_stack "$dir" ;;
  all)
    "${REPO_LOCAL}/scripts/sync.sh" "$dir"
    sync_plugins "$dir"
    [ "$dir" = push ] && sync_stack push || true ;;
  *)
    echo "usage: deploy.sh {repo|plugins|stack|all} [push|pull]" >&2
    exit 2 ;;
esac
echo ">> done"
