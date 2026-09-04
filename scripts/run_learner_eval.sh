#!/usr/bin/env bash
# Wrapper: ask Swarm every question in a learner-eval set, under the compose-derived
# environment (never hand-copied variables) and with the standing guard around the model
# step. No secret is needed here -- the grader is the Proxmox API, offline, in a separate
# step.
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

OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-hive-ollama-1}"

memguard() {
  echo "MEMGUARD $1 $(date -u +%FT%TZ) $(grep MemAvailable /proc/meminfo | awk '{printf "%.1f GiB", $2/1048576}') $(grep full /proc/pressure/memory | cut -d' ' -f2)"
}

# Why this exists, and why MemAvailable was never enough.
#
# On 2026-09-04 `hive-ollama-1` was killed at 11:00:12Z with SIGKILL by earlyoom, which
# kills the largest process before the kernel OOM killer and so never sets Docker's
# OOMKilled flag. It stayed dead for 2h17m. The old guard checked MemAvailable and memory
# PSI, and both looked EXCELLENT immediately afterwards -- because the thing that had been
# using the memory was gone. The guard was reading the consequence of the failure as
# health.
#
# So liveness is asserted directly, against the daemon the run will actually use, and it
# ABORTS rather than warns: a run against a dead model endpoint must never be able to
# write a number. This is the residency-guard shape banked in board/journal.md
# ("must use `docker exec hive-ollama-1 ollama ps`, MemAvailable, memory PSI ... not host
# `ollama ps`") -- banked then, never applied here, which is the actual reason this slipped.
modelguard() {
  local when="$1" listing

  if ! listing="$(docker exec "$OLLAMA_CONTAINER" ollama list 2>&1)"; then
    echo "MODELGUARD $when FAIL $(date -u +%FT%TZ) — '$OLLAMA_CONTAINER' did not answer 'ollama list'." >&2
    echo "  A dead or unreachable model daemon produces error rows or silent zeros, not a" >&2
    echo "  measurement. Check: docker inspect $OLLAMA_CONTAINER --format '{{.State.ExitCode}}'" >&2
    echo "  (exit 137 with OOMKilled=false means earlyoom, not the kernel OOM killer)." >&2
    echo "  Output was: $listing" >&2
    return 1
  fi

  # Every model this run's configuration will actually reach for. Derived from the
  # compose-resolved environment, never a hand-kept list, so adding a panel member cannot
  # silently escape the check.
  local required=() missing=() model
  IFS=',' read -r -a required <<<"${SWARM_CONSILIUM_PANEL:-}"
  required+=("${SWARM_CONSILIUM_JUDGE:-}" "${SWARM_TIER_GATE_ENTAIL_MODEL:-}")

  for model in "${required[@]}"; do
    model="$(printf '%s' "$model" | tr -d '[:space:]')"
    [ -z "$model" ] && continue
    printf '%s' "$listing" | grep -qF "$model" || missing+=("$model")
  done

  if [ "${#missing[@]}" -gt 0 ]; then
    echo "MODELGUARD $when FAIL $(date -u +%FT%TZ) — configured but not present: ${missing[*]}" >&2
    return 1
  fi

  # `ollama ps` is what is RESIDENT right now; printed, not required. Ollama loads on
  # demand, so nothing is resident before the first ask and an empty list here is normal.
  echo "MODELGUARD $when OK $(date -u +%FT%TZ) present=$(printf '%s' "$listing" | tail -n +2 | wc -l) resident=$(docker exec "$OLLAMA_CONTAINER" ollama ps 2>/dev/null | tail -n +2 | wc -l)"
}

memguard before
eval "$("$hive/scripts/kernel-measure-env")"
modelguard before
echo "CONDITIONS swarm=$(git -C "$hive/../swarm" rev-parse --short HEAD) swarm_dirty=$(git -C "$hive/../swarm" status --porcelain | wc -l) hive=$(git -C "$hive" rev-parse --short HEAD) hive_dirty=$(git -C "$hive" status --porcelain | wc -l) db=$SWARM_DB_NAME ml=$SWARM_ML_ADDRESS panel=$SWARM_CONSILIUM_PANEL judge=$SWARM_CONSILIUM_JUDGE"

LEARNER_EVAL_FILE="$set" LEARNER_EVAL_OUT="$out" \
  MIX_ENV=dev mise exec -C "$kernel" -- mix run --no-start "$hive/scripts/learner_eval_run.exs"

memguard after

# The after-check is the one that matters most: a daemon that died PART WAY through leaves
# a half-real output file, and every row after the death is an error or a zero. The output
# is already on disk by now, so instead of pretending otherwise, mark it void beside itself
# so the grader and the journal cannot mistake it for a measurement.
if ! modelguard after; then
  cat >"$out.VOID" <<VOID
VOID $(date -u +%FT%TZ)
The model daemon did not pass the liveness check AFTER this run, so rows in
$(basename "$out")
may have been produced against a dead endpoint. Do not grade this file and do not
record a number from it. Re-run once the daemon is healthy.
VOID
  echo "MODELGUARD after: wrote $out.VOID — this run is not a measurement" >&2
  exit 3
fi
