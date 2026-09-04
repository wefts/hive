#!/usr/bin/env bash
# THERE ARE TWO OLLAMA DAEMONS ON THIS BOX. This is the only place that knows it, and
# everything that touches a model sources this file instead of remembering.
#
#   host       a host-installed ollama on localhost:11434, restarted by its supervisor
#   container  hive-ollama-1, reached by the ML sidecar as `ollama:11434`, NOT published
#              to any host port -- so localhost:11434 is NEVER the container
#
# They hold separate model caches and separate GPU memory. `docker port hive-ollama-1`
# prints nothing, which is the proof: a host `ollama ps` says nothing whatsoever about
# what the serve path has resident.
#
# ## Why this is a guard and not a comment
#
# This hazard has bitten twice, both times someone who knew there were two daemons:
#
#   2026-09-02  a host `ollama ps` was read as the container's residency -> FOUR wrong
#               explanations of a memory event
#   2026-09-04  critic calls went to the host daemon while the serve path used the
#               container; earlyoom killed models under both -> 2h17m of dead model and
#               two critic reviews silently lost
#
# Care did not prevent either. So the rule is mechanical: BOTH DAEMONS MUST NOT HOLD
# MODELS AT THE SAME TIME. That is the condition that invited the kill -- two resident
# copies of large models on one GPU -- and it is one command to check.
#
# Usage (source it, then call):
#
#   . scripts/ollama_daemons.sh
#   ollama_daemon_report            # print per-daemon residency
#   ollama_daemon_require_exclusive # exit 2 if both hold models
#   ollama_daemon_identity          # one line naming the serve daemon, for a condition hash
#
# Or run it directly for the report plus the exclusivity check.

OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-hive-ollama-1}"
OLLAMA_HOST_URL="${OLLAMA_HOST_URL:-http://localhost:11434}"

# Resident model names on the host daemon, one per line. Empty if it is not answering --
# absence of a daemon is not an error here, only a fact to report.
ollama_host_resident() {
  curl -s -m 5 "$OLLAMA_HOST_URL/api/ps" 2>/dev/null |
    python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for m in d.get("models") or []: print(m.get("name") or m.get("model") or "?")' 2>/dev/null || true
}

ollama_container_resident() {
  docker exec "$OLLAMA_CONTAINER" ollama ps 2>/dev/null | tail -n +2 | awk 'NF{print $1}' || true
}

ollama_daemon_report() {
  local h c
  h="$(ollama_host_resident)"
  c="$(ollama_container_resident)"
  # Reported SEPARATELY and always both, so a reader cannot mistake one for the other.
  echo "OLLAMA host      resident=$(printf '%s' "$h" | grep -c . || true) [$(printf '%s' "$h" | paste -sd, -)]"
  echo "OLLAMA container resident=$(printf '%s' "$c" | grep -c . || true) [$(printf '%s' "$c" | paste -sd, -)] ($OLLAMA_CONTAINER)"
}

# The rule. Refuses rather than warns: a measurement or critic call that proceeds while
# both daemons are loaded is competing with itself for the GPU, and the loser is whichever
# process earlyoom judges largest.
ollama_daemon_require_exclusive() {
  local hn cn
  hn="$(ollama_host_resident | grep -c . || true)"
  cn="$(ollama_container_resident | grep -c . || true)"

  if [ "$hn" -gt 0 ] && [ "$cn" -gt 0 ]; then
    echo "OLLAMA GUARD FAIL: both daemons hold models (host=$hn container=$cn)." >&2
    ollama_daemon_report >&2
    echo "  Two resident copies of large models on one GPU is the condition that got the" >&2
    echo "  model daemon SIGKILLed by earlyoom on 2026-09-04. Free one before proceeding:" >&2
    echo "    host:      curl -s $OLLAMA_HOST_URL/api/generate -d '{\"model\":\"<m>\",\"keep_alive\":0}'" >&2
    echo "    container: docker exec $OLLAMA_CONTAINER ollama stop <model>" >&2
    return 2
  fi
  return 0
}

# Identity of the daemon that ANSWERS THE SERVE PATH, for the condition hash. A
# measurement that does not say which ollama answered cannot be reproduced.
ollama_daemon_identity() {
  local id ver
  id="$(docker inspect -f '{{.Id}}' "$OLLAMA_CONTAINER" 2>/dev/null | cut -c1-12)"
  ver="$(docker exec "$OLLAMA_CONTAINER" ollama --version 2>/dev/null | tr -d '\r' | tail -1)"
  echo "container:${OLLAMA_CONTAINER}:${id:-unknown}:${ver:-unknown}"
}

# Direct invocation: report, then enforce.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  set -uo pipefail
  ollama_daemon_report
  echo "serve daemon: $(ollama_daemon_identity)"
  ollama_daemon_require_exclusive
fi
