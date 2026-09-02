#!/usr/bin/env bash
# Nightly synthetic comprehension canary.
#
# Runs invented contrastive fixtures through the local Swarm fleet and asks
# Gemini to grade direction preservation. This is out-of-band evaluation, not
# the serve path. Inputs are synthetic; outputs go under gitignored hive/tmp.
#
# Cron example:
#   45 1 * * *  <workspace>/hive/scripts/nightly_contrastive_canary.sh
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace="$(cd "${here}/.." && pwd)"
log_dir="${here}/tmp/contrastive-canary"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log="${log_dir}/nightly_${stamp}.log"

mkdir -p "$log_dir"

{
  echo "== synthetic contrastive canary ${stamp} =="
  export SWARM_ENV="${SWARM_ENV:-staging}"
  eval "$("${here}/scripts/kernel-measure-env")"

  if [ -f "${here}/secrets.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${here}/secrets.env"
    set +a
  fi

  : "${GEMINI_API_KEY:?GEMINI_API_KEY must be set in hive/secrets.env or the environment}"

  cd "${workspace}/swarm/kernel"
  MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/synthetic_contrastive_canary.exs
  echo "== synthetic contrastive canary done ${stamp} =="
} >>"$log" 2>&1
