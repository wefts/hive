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
# Canon: docs/standards/verification.md, "A check that did not run is not a check that
# passed" -- assert the dependency, never its side effects.
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
#   modelguard before warm      liveness + presence + PROVE each model runs and is resident
#   modelguard after  liveness   liveness + presence only
#
# The after-check deliberately does NOT re-warm. Its job is to detect a daemon that died
# PART WAY THROUGH, and re-loading every model to prove that costs a minute and ~43 GiB of
# residency for no measurement value. Measured on the first run that used it: the before
# guard took 53s, the after guard took 61s re-loading models the run had already finished
# with. Liveness is the question after; loadability is the question before.
modelguard() {
  local when="$1" mode="${2:-warm}" listing

  # The two-daemon rule, from the one place that knows there are two
  # (scripts/ollama_daemons.sh). Both resident at once is the condition that got the model
  # daemon SIGKILLed by earlyoom, and it has cost us twice.
  # shellcheck source=/dev/null
  . "$hive/scripts/ollama_daemons.sh"
  ollama_daemon_report
  ollama_daemon_require_exclusive || return 1

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
  local declared=() required=() missing=() model
  IFS=',' read -r -a declared <<<"${SWARM_CONSILIUM_PANEL:-}"
  declared+=("${SWARM_CONSILIUM_JUDGE:-}" "${SWARM_TIER_GATE_ENTAIL_MODEL:-}")

  # Deduplicated: the entail model is routinely also a panel member, and warming it twice
  # is wasted minutes and a confusing `resident_verified` line.
  for model in "${declared[@]}"; do
    model="$(printf '%s' "$model" | tr -d '[:space:]')"
    [ -z "$model" ] && continue
    [[ " ${required[*]-} " == *" $model "* ]] && continue
    required+=("$model")
  done

  for model in "${required[@]}"; do
    printf '%s' "$listing" | grep -qF "$model" || missing+=("$model")
  done

  if [ "${#missing[@]}" -gt 0 ]; then
    echo "MODELGUARD $when FAIL $(date -u +%FT%TZ) — configured but not present: ${missing[*]}" >&2
    return 1
  fi

  if [ "$mode" != "warm" ]; then
    echo "MODELGUARD $when OK $(date -u +%FT%TZ) liveness-only present=$(printf '%s' "$listing" | tail -n +2 | wc -l) required=${required[*]}"
    return 0
  fi

  # RESIDENCY, not just presence. "Pulled" is a weaker claim than "can actually answer":
  # a model can be listed and still fail to load (too large now that something else holds
  # the memory), which is precisely the state the outage left behind. Ollama loads on
  # demand, so residency is not something to wait for -- it is something to CAUSE and then
  # verify. Each required model is warmed with a one-token generate and must then appear in
  # `ollama ps`. That makes this a proof the model can run, on the daemon the run will use.
  local resident=()
  for model in "${required[@]}"; do
    if ! docker exec "$OLLAMA_CONTAINER" \
      ollama run "$model" --keepalive "${MODELGUARD_KEEPALIVE:-10m}" "ok" >/dev/null 2>&1; then
      echo "MODELGUARD $when FAIL $(date -u +%FT%TZ) — '$model' is present but would not RUN." >&2
      echo "  Listed is not loadable: check free memory, and whether another daemon holds" >&2
      echo "  the GPU (a host ollama and this container both resident is what invited the" >&2
      echo "  earlyoom kill on 2026-09-04)." >&2
      return 1
    fi
    if ! docker exec "$OLLAMA_CONTAINER" ollama ps 2>/dev/null | grep -qF "$model"; then
      echo "MODELGUARD $when FAIL $(date -u +%FT%TZ) — '$model' ran but is not resident in 'ollama ps'." >&2
      echo "  It loaded and was evicted immediately, which means the run would thrash." >&2
      return 1
    fi
    resident+=("$model")
  done

  echo "MODELGUARD $when OK $(date -u +%FT%TZ) present=$(printf '%s' "$listing" | tail -n +2 | wc -l) resident_verified=${resident[*]}"
}

memguard before
eval "$("$hive/scripts/kernel-measure-env")"
modelguard before warm
echo "CONDITIONS swarm=$(git -C "$hive/../swarm" rev-parse --short HEAD) swarm_dirty=$(git -C "$hive/../swarm" status --porcelain | wc -l) hive=$(git -C "$hive" rev-parse --short HEAD) hive_dirty=$(git -C "$hive" status --porcelain | wc -l) db=$SWARM_DB_NAME ml=$SWARM_ML_ADDRESS panel=$SWARM_CONSILIUM_PANEL judge=$SWARM_CONSILIUM_JUDGE"

LEARNER_EVAL_FILE="$set" LEARNER_EVAL_OUT="$out" \
  MIX_ENV=dev mise exec -C "$kernel" -- mix run --no-start "$hive/scripts/learner_eval_run.exs"

memguard after

# The after-check is the one that matters most: a daemon that died PART WAY through leaves
# a half-real output file, and every row after the death is an error or a zero. The output
# is already on disk by now, so instead of pretending otherwise, mark it void beside itself
# so the grader and the journal cannot mistake it for a measurement.
if ! modelguard after liveness; then
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
