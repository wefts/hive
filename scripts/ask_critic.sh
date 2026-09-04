#!/usr/bin/env bash
# One decorrelated critic call, with the two checks that were missing when three reviews
# were silently lost on 2026-09-04.
#
#   scripts/ask_critic.sh <model> <prompt-file> <output-file>
#
# 1. IT SAYS WHICH DAEMON IT WILL USE, and defaults to the CONTAINER (hive-ollama-1) --
#    the same daemon the serve path uses. The old habit was localhost:11434, which is the
#    HOST daemon and a different GPU tenant; a critic there competes with the serve path
#    invisibly. Override with OLLAMA_CRITIC_DAEMON=host only deliberately.
#
# 2. IT CHECKS THE CALL, not the existence of its output file. Three "reviews" on ADR-22
#    were a traceback, a traceback, and zero bytes, sitting in a directory looking like a
#    council, because the caller redirected stdout and never looked at the exit status.
#    A failure here is loud and writes NO output file, so an absent review can never be
#    mistaken for a critic who declined to find anything.
#
# Canon: docs/standards/verification.md, "A check that did not run is not a check that
# passed".
set -uo pipefail

model="${1:?model, e.g. gemma4:31b}"
prompt_file="${2:?path to a prompt file}"
out="${3:?path to write the review to}"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "$here/ollama_daemons.sh"

daemon="${OLLAMA_CRITIC_DAEMON:-container}"

[ -s "$prompt_file" ] || { echo "ask_critic: prompt file is empty: $prompt_file" >&2; exit 1; }

echo "CRITIC daemon=$daemon model=$model prompt=$(wc -c <"$prompt_file") bytes"
ollama_daemon_report
ollama_daemon_require_exclusive || exit 2

mem() { grep MemAvailable /proc/meminfo | awk '{printf "%.1f GiB", $2/1048576}'; }
echo "MEMGUARD before $model $(date -u +%FT%TZ) $(mem) psi=$(grep full /proc/pressure/memory | cut -d' ' -f2)"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

case "$daemon" in
  container)
    # No host port is published for the container, so this is the only way in. stdin
    # rather than an argv prompt: a review prompt is kilobytes and full of quotes.
    docker exec -i "$OLLAMA_CONTAINER" ollama run "$model" <"$prompt_file" >"$tmp" 2>&1
    rc=$?
    ;;
  host)
    echo "CRITIC WARNING: using the HOST daemon; it is a different GPU tenant from the" >&2
    echo "  serve path and this call will compete with any measurement in flight." >&2
    body="$(python3 -c 'import json,sys
print(json.dumps({"model":sys.argv[1],"prompt":open(sys.argv[2]).read(),"stream":False,
                  "options":{"num_ctx":8192,"temperature":0.2}}))' "$model" "$prompt_file")"
    http="$(curl -s -m 900 -o "$tmp.raw" -w '%{http_code}' "$OLLAMA_HOST_URL/api/generate" \
      -H 'Content-Type: application/json' -d "$body")"
    rc=$?
    if [ "$rc" -eq 0 ] && [ "$http" = "200" ]; then
      python3 -c 'import json,sys; print((json.load(open(sys.argv[1])).get("response") or "").strip())' \
        "$tmp.raw" >"$tmp" || rc=1
    else
      echo "CRITIC $model FAILED curl_rc=$rc http=$http" >&2
      head -c 300 "$tmp.raw" >&2; echo >&2
      rc=1
    fi
    rm -f "$tmp.raw"
    ;;
  *)
    echo "ask_critic: OLLAMA_CRITIC_DAEMON must be 'container' or 'host', got '$daemon'" >&2
    exit 1
    ;;
esac

if [ "$rc" -ne 0 ]; then
  echo "CRITIC $model FAILED rc=$rc — NO REVIEW OBTAINED, nothing written to $out" >&2
  head -c 400 "$tmp" >&2; echo >&2
  exit 1
fi

# An empty body from a healthy-looking call is the exact shape of the lost glm review.
if [ ! -s "$tmp" ]; then
  echo "CRITIC $model FAILED — call succeeded but the review is EMPTY. NO REVIEW OBTAINED." >&2
  exit 1
fi

mv -f "$tmp" "$out"
trap - EXIT
echo "CRITIC OK daemon=$daemon model=$model bytes=$(wc -c <"$out") -> $out"
echo "MEMGUARD after  $model $(date -u +%FT%TZ) $(mem) psi=$(grep full /proc/pressure/memory | cut -d' ' -f2)"
