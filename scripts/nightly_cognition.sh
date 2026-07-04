#!/usr/bin/env bash
# Nightly bounded cognitive-loop run (operator decision 2026-07-03): enrichment +
# entity-resolution crawl the staging corpus in DOSED nightly increments instead of
# a continuous daemon (ADR-13 cost-asymmetry; council GO covers bounded
# snapshot-protected operation only — a week of stable nightly gauges is the
# de-facto equilibrium evidence before any always-on promotion).
#
# Safety posture, in order:
#   1. flock — never two runs at once;
#   2. pg_dump snapshot BEFORE mutating (rotated, keep 7);
#   3. LOOP_MODE=real EXPLICIT (the script's env var — a bare MODE= is ignored!);
#   4. calibrated gates (reward 0.65, ER vec 0.90 — board journal 2026-06-30);
#   5. wall-clock timeout;
#   6. the harness's own circuit-breaker; a fired breaker leaves
#      $LOG_DIR/BREAKER-<date> and exits non-zero.
#
# Budget: CYCLES×ENRICH_ROUNDS×MAX_PER_PASS sources/night (default 2×4×5 = 40,
# ~80 min at ~2 min/source on the single GPU). Override via env. No secrets used:
# enrichment reads the graph, generates via the local fleet, embeds via ml.
#
# Cron (installed on this host):  30 0 * * *  <workspace>/hive/scripts/nightly_cognition.sh
set -euo pipefail

# cron runs with a minimal PATH (no ~/.local/bin) — `mise` lives there and the
# 00:30 run silently no-op'd on 2026-07-04 ("env: mise: No such file or directory";
# the snapshot still ran, so it looked half-alive). Make the toolchain reachable.
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # workspace root
LOG_DIR="${LOG_DIR:-$here/tmp/cogloop}"
SNAP_DIR="${SNAP_DIR:-$here/tmp/snapshots}"
STAMP="$(date +%Y%m%d_%H%M)"
mkdir -p "$LOG_DIR" "$SNAP_DIR"
LOG="$LOG_DIR/nightly_${STAMP}.log"

exec 9>"$LOG_DIR/.lock"
flock -n 9 || { echo "another run holds the lock — skipping" >>"$LOG"; exit 0; }

{
  echo "== nightly cognition $STAMP =="

  # 1. snapshot + rotate (keep 7)
  docker exec hive-postgres-1 pg_dump -U swarm -Fc swarm_staging \
    > "$SNAP_DIR/nightly_cog_${STAMP}.dump"
  ls -t "$SNAP_DIR"/nightly_cog_*.dump 2>/dev/null | tail -n +8 | xargs -r rm -f

  # 2. resolve the ml endpoint dynamically (container IP changes across recreates)
  ML_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' hive-ml-1)"

  # 3. the bounded run
  cd "$here/swarm/kernel"
  timeout "${WALL_CLOCK:-4h}" env \
    LOOP_MODE=real \
    CYCLES="${CYCLES:-2}" \
    ENRICH_ROUNDS="${ENRICH_ROUNDS:-4}" \
    MAX_PER_PASS="${MAX_PER_PASS:-5}" \
    SWARM_DB_NAME=swarm_staging \
    SWARM_ENV=staging \
    SWARM_ML_ADDRESS="${ML_IP}:50051" \
    SWARM_ENRICH_THRESHOLD="${SWARM_ENRICH_THRESHOLD:-0.65}" \
    SWARM_ER_VEC_THRESHOLD="${SWARM_ER_VEC_THRESHOLD:-0.90}" \
    MIX_ENV=dev \
    mise exec -- mix run --no-start ../../hive/scripts/cognitive_loop.exs

  echo "== done $(date +%H:%M) =="
} >>"$LOG" 2>&1 || {
  echo "== FAILED/BREAKER $(date +%H:%M) ==" >>"$LOG"
  touch "$LOG_DIR/BREAKER-${STAMP}"
  exit 1
}

# a breaker that halted-but-exited-0 still counts as an incident
if grep -qi "breaker" "$LOG" && ! grep -q "no breaker" "$LOG"; then
  touch "$LOG_DIR/BREAKER-${STAMP}"
  exit 1
fi
