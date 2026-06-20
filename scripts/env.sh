# Shared config for hive deploy/orchestration scripts (PRIVATE layer, outside
# the public repo). Source me: `. scripts/env.sh`
#
# The public repo has its own scripts/env.sh for repo-only sync; this one knows
# the private hive (plugins + stack) and where the sibling public repo lives.

# SSH target. Empty SSH_USER => ssh config resolves the user (spark host alias
# authenticates without user@).
export SPARK_HOST="${SPARK_HOST:-dgx_spark}"
export SSH_USER="${SSH_USER:-}"
export SPARK="${SSH_USER:+${SSH_USER}@}${SPARK_HOST}"

# Hive root = parent of this scripts/ dir.
HIVE_LOCAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export HIVE_LOCAL

# Public repo lives next to the hive.
REPO_LOCAL="$(cd "${HIVE_LOCAL}/../swarm" && pwd)"
export REPO_LOCAL

# Remote layout (system architecture §13). Mirror of the local checkout.
export WORKSPACE_REMOTE="${WORKSPACE_REMOTE:-\$HOME/Swarm}"
export REPO_REMOTE="${REPO_REMOTE:-\$HOME/Swarm/swarm}"
export HIVE_REMOTE="${HIVE_REMOTE:-\$HOME/Swarm/hive}"
export PLUGINS_REMOTE="${PLUGINS_REMOTE:-\$HOME/Swarm/hive/plugins}"

# Regenerated artifacts — never transferred.
BUILD_EXCLUDES=(
  --exclude '.venv'    --exclude '__pycache__' --exclude '*.pyc'
  --exclude '.ruff_cache' --exclude '.mypy_cache' --exclude '.pytest_cache'
  --exclude '_build'   --exclude 'deps'        --exclude '.elixir_ls'
  --exclude 'node_modules'
)
export BUILD_EXCLUDES

# SECURITY: never leaves this machine, on ANY target. Secrets + real corpus +
# scratch + per-machine config. Hard backstop, not just convention.
SECURITY_EXCLUDES=(
  --exclude 'secrets.env'
  --exclude '.env'
  --exclude 'data'
  --exclude 'tmp'
)
export SECURITY_EXCLUDES
