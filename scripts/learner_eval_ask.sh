#!/usr/bin/env bash
# Wrapper: source the gitignored secrets INSIDE the script (never on a command
# line, never echoed), then run the learner. Same pattern as run_glpi_eval.sh.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a
# shellcheck source=/dev/null
. "$here/secrets.env"
set +a
exec python3 "$here/scripts/learner_eval_ask.py" "$@"
