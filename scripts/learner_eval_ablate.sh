#!/usr/bin/env bash
# Measure the placement fixes INCREMENTALLY, one at a time, on the same 18 controls.
#
# Landing three fixes and measuring once produces a number nobody can attribute. The
# trace predicted cue gap 13/18 and binding 2/18 bound-to-expected; a combined number
# checks neither prediction, and the next question is always "which one do we keep?".
#
# Each state swaps only the two world-map files, recompiles, runs the trace, restores.
# The trace's own condition hash records the swarm revision and dirty flag per run, so
# the artifacts say for themselves which build produced them.
#
#   A  cue only
#   B  cue + candidate binding
#   C  cue + binding + domain precedence
#
# Baseline (no fixes) is the already-recorded trace_controls_v2.jsonl.
#
# Usage: scripts/learner_eval_ablate.sh <state-dir> <out-dir> [A B C]
set -euo pipefail

states="${1:?state dir holding domain.<S>.ex / coverage.<S>.ex}"
out="${2:?output dir}"
shift 2
which=("${@:-A B C}")

hive="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kernel="$hive/../swarm/kernel"
wm="$kernel/lib/swarm/world_map"
set_file="$hive/tmp/learner-eval/set_frozen.jsonl"

restore() {
  cp "$states/domain.C.ex" "$wm/domain.ex"
  cp "$states/coverage.C.ex" "$wm/coverage.ex"
  echo "ablate: restored state C" >&2
}
trap restore EXIT

mkdir -p "$out"
export SWARM_ENV="${SWARM_ENV:-staging}"
eval "$("$hive/scripts/kernel-measure-env")"

for s in "${which[@]}"; do
  cov="$states/coverage.$s.ex"
  dom="$states/domain.$s.ex"
  [ -f "$dom" ] || dom="$states/domain.C.ex"   # every state carries the cue fix
  cp "$dom" "$wm/domain.ex"
  cp "$cov" "$wm/coverage.ex"

  echo "=== ablate state $s ===" >&2
  LEARNER_TRACE_REPEAT=0 \
    LEARNER_TRACE_SET="$set_file" \
    LEARNER_TRACE_OUT="$out/trace_state_$s.jsonl" \
    MIX_ENV=dev mise exec -C "$kernel" -- mix run --no-start "$hive/scripts/learner_eval_trace.exs" \
    > "$out/trace_state_$s.log" 2>&1

  tail -2 "$out/trace_state_$s.log" >&2
done
