#!/usr/bin/env bash
# Network-map Phase-2 refresh (network-map-nightly-refresh card). Rebuilds the authoritative
# IaC + wiki-table facts + wiki∩repo corroboration in the world-map, idempotently. Safe to cron
# (own slot, or a step after nightly_cognition.sh).
#
#   Steps: purge iac/wiki origins → clone+parse each repo → load → export+parse wiki tables → load
#          → corroborate. Purge+reload is fully re-derivable; Phase-1 (wiki prose) is preserved.
#
# LEAK POSTURE: the repo CLONE URLs (intranet) live in a gitignored list, NOT this committed file.
# Cloned repos + facts JSON go to tmp/ (gitignored). Only DISTILLED facts enter the graph (a Docker
# volume, never git). No external model touches repo content. SSH + corpus reads are read-only.
#
# Config (gitignored): tmp/netmap.repos — one full git clone URL per line (e.g.
#   git@<intranet-git-host>:group/repo.git). Lines starting with # ignored.
set -euo pipefail

# cron has a minimal PATH — reach mise (elixir) and uv (python), like nightly_cognition.sh.
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # hive/ (script is in scripts/netmap/)
TMP="${NETMAP_TMP:-$here/tmp}"
REPO_LIST="${NETMAP_REPOS:-$TMP/netmap.repos}"
CLONE_DIR="${NETMAP_CLONE_DIR:-$TMP/iac_repos}"
KERNEL="${KERNEL:-hive-kernel-1}"
NETMAP="$here/scripts/netmap"
LOG_DIR="${LOG_DIR:-$TMP/netmap}"
STAMP="$(date +%Y%m%d_%H%M)"
TIMEOUT="${NETMAP_TIMEOUT:-900}"
mkdir -p "$LOG_DIR" "$CLONE_DIR"
LOG="$LOG_DIR/netmap_${STAMP}.log"

exec 9>"$LOG_DIR/.lock"
flock -n 9 || { echo "netmap: another run holds the lock — skipping"; exit 0; }

[ -f "$REPO_LIST" ] || { echo "netmap: no repo list at $REPO_LIST (gitignored; one clone URL/line) — nothing to do" | tee -a "$LOG"; exit 0; }

run_rpc() {  # run a committed .exs inside the kernel release. NO `-i` + stdin</dev/null: a
  # `docker exec -i` inside the `while read` loop would EAT the repo-list stdin (loop input).
  script="$NETMAP/$1"

  if [ -n "${SWARM_DB_NAME:-}" ]; then
    rpc_env_args=()
    for var in SWARM_DB_NAME SWARM_ENV NETMAP_SOURCE_ID WIKI_SOURCE_ID; do
      val="${!var:-}"
      [ -n "$val" ] && rpc_env_args+=(-e "$var=$val")
    done

    docker exec "${rpc_env_args[@]}" "$KERNEL" /app/bin/swarm eval \
      "$(printf '{:ok, _} = Application.ensure_all_started(:ecto_sql)\n{:ok, _} = Application.ensure_all_started(:postgrex)\n{:ok, _} = Swarm.Repo.start_link()\n'; cat "$script")" </dev/null
  else
    docker exec "$KERNEL" /app/bin/swarm rpc "$(cat "$script")" </dev/null
  fi
}

{
  echo "== netmap refresh $STAMP =="
  export GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes"

  echo "-- purge prior iac/wiki origins --"
  run_rpc purge_netmap.exs

  echo "-- clone + parse + load each repo --"
  while IFS= read -r url; do
    [ -z "$url" ] && continue
    case "$url" in \#*) continue ;; esac
    name="$(basename "$url" .git)"
    dest="$CLONE_DIR/$name"
    rm -rf "$dest"
    if git clone --depth 1 -q "$url" "$dest" 2>>"$LOG"; then
      uv run --quiet --with pyyaml python "$NETMAP/parse_iac.py" "$dest" > "$TMP/netmap_facts.json" 2>>"$LOG"
      docker cp "$TMP/netmap_facts.json" "$KERNEL:/tmp/netmap_facts.json" >/dev/null
      run_rpc load_facts.exs
    else
      echo "clone FAILED: $name"
    fi
  done < "$REPO_LIST"

  echo "-- wiki inventory tables --"
  run_rpc export_tables.exs
  docker cp "$KERNEL:/tmp/wiki_bodies.json" "$TMP/wiki_bodies.json" >/dev/null
  uv run --quiet python "$NETMAP/parse_wiki_tables.py" "$TMP/wiki_bodies.json" > "$TMP/netmap_facts.json" 2>>"$LOG"
  docker cp "$TMP/netmap_facts.json" "$KERNEL:/tmp/netmap_facts.json" >/dev/null
  run_rpc load_facts.exs

  echo "-- wiki API dynamic pages (rendered HTML tables the ingest strips) --"
  if bash "$NETMAP/fetch_wiki_pages.sh" >>"$LOG" 2>&1; then
    uv run --quiet --with beautifulsoup4 python "$NETMAP/parse_wiki_html.py" "$TMP/wiki_html" > "$TMP/netmap_facts.json" 2>>"$LOG"
    docker cp "$TMP/netmap_facts.json" "$KERNEL:/tmp/netmap_facts.json" >/dev/null
    run_rpc load_facts.exs
  else
    echo "wiki-api fetch skipped (no page list / login failed) — see $LOG"
  fi

  echo "-- wiki∩repo corroboration --"
  run_rpc corroborate.exs

  echo "== netmap refresh done $STAMP =="
} 2>&1 | tee -a "$LOG"

# tidy transient facts (keep clones for inspection; they're gitignored)
rm -f "$TMP/netmap_facts.json" "$TMP/wiki_bodies.json"
