#!/usr/bin/env bash
# Wrapper: ask Swarm every question in a learner-eval set, under the compose-derived
# environment (never hand-copied variables) and with the standing memory guard around
# the model step. No secret is needed here -- the grader is the Proxmox API, offline,
# in a separate step.
#
#   scripts/run_learner_eval.sh tmp/learner-eval/set_live.jsonl tmp/learner-eval/run_live.jsonl
set -euo pipefail
export SWARM_ENV="${SWARM_ENV:-staging}"
hive="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kernel="$hive/../swarm/kernel"
# Absolute: the runner executes with the kernel as its working directory, so a path
# relative to the shell that launched this would resolve somewhere else entirely.
set="$(readlink -f "${1:?input set jsonl}")"
out="$(cd "$(dirname "${2:?output jsonl path}")" && pwd)/$(basename "$2")"

guard() {
  echo "MEMGUARD $1 $(date -u +%FT%TZ) $(grep MemAvailable /proc/meminfo | awk '{printf "%.1f GiB", $2/1048576}') $(grep full /proc/pressure/memory | cut -d' ' -f2)"
}

guard before
eval "$("$hive/scripts/kernel-measure-env")"
echo "CONDITIONS swarm=$(git -C "$hive/../swarm" rev-parse --short HEAD) swarm_dirty=$(git -C "$hive/../swarm" status --porcelain | wc -l) hive=$(git -C "$hive" rev-parse --short HEAD) hive_dirty=$(git -C "$hive" status --porcelain | wc -l) db=$SWARM_DB_NAME ml=$SWARM_ML_ADDRESS panel=$SWARM_CONSILIUM_PANEL judge=$SWARM_CONSILIUM_JUDGE"

LEARNER_EVAL_FILE="$set" LEARNER_EVAL_OUT="$out" \
  MIX_ENV=dev mise exec -C "$kernel" -- mix run --no-start "$hive/scripts/learner_eval_run.exs"

guard after
