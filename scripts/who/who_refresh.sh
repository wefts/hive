#!/usr/bin/env bash
# Who-is-who refresh (world-map master-plan E1). Rebuilds the org-directory people/teams/roles/sites
# substrate in the world-map from LDAP, idempotently. Safe to cron (own slot).
#
#   Steps: run the host-side connector (anonymous bind, allowlist) → distilled who_facts.json →
#          docker cp into the kernel → load (ATOMIC FULL REPLACE — departed/moved people vanish).
#
# LEAK POSTURE: the directory host + base DN are intranet specifics — they come from ENV
# (SWARM_LDAP_HOST / SWARM_LDAP_BASE_DN / optional SWARM_LDAP_PORT), never this committed file; put
# them in a gitignored env (hive/env/<stage>.env or the shell). The connector fetches ONLY the
# ADR-16 allowlist (no auth/system attrs) and prints only aggregate counts. Facts JSON goes to tmp/
# (gitignored); only DISTILLED facts (allowlisted attrs + org relations) enter the graph (a Docker
# volume, never git). No external model touches directory content.
set -euo pipefail

# cron has a minimal PATH — reach uv (python) + docker.
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # hive/ (script is in scripts/who/)
TMP="${WHO_TMP:-$here/tmp}"
WHO="$here/scripts/who"
KERNEL="${KERNEL:-hive-kernel-1}"
LOG_DIR="${LOG_DIR:-$TMP/who}"
STAMP="$(date +%Y%m%d_%H%M)"
FACTS="$TMP/who_facts.json"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/who_${STAMP}.log"

exec 9>"$LOG_DIR/.lock"
flock -n 9 || { echo "who: another run holds the lock — skipping"; exit 0; }

# The directory host/base-DN are per-stage NON-secret instance facts kept in the (gitignored) stage
# env, the single config home. Pull ONLY the SWARM_LDAP_* keys into the environment (never source
# the whole file) unless already set in the shell. Stage defaults to staging; override via SWARM_ENV.
STAGE_ENV="${WHO_STAGE_ENV:-$here/env/${SWARM_ENV:-staging}.env}"
if [ -f "$STAGE_ENV" ]; then
  set -a
  # SWARM_LDAP_* (directory coords) + schema/curated overlay spec paths.
  eval "$(grep -E '^(SWARM_LDAP_(HOST|BASE_DN|PORT)|SWARM_WHO_LDAP_SCHEMA|SWARM_WHO_GROUPS|SWARM_WHO_SERVICES)=' "$STAGE_ENV" || true)"
  set +a
fi

# Resolve relative overlay-spec paths against the hive root so cron (minimal CWD) still finds them.
for v in SWARM_WHO_LDAP_SCHEMA SWARM_WHO_GROUPS SWARM_WHO_SERVICES; do
  val="${!v:-}"
  if [ -n "$val" ]; then case "$val" in /*) : ;; *) export "$v"="$here/$val" ;; esac; fi
done

if [ -z "${SWARM_LDAP_HOST:-}" ] || [ -z "${SWARM_LDAP_BASE_DN:-}" ]; then
  echo "who: SWARM_LDAP_HOST / SWARM_LDAP_BASE_DN not set (intranet specifics; gitignored env) — nothing to do" | tee -a "$LOG"
  exit 0
fi

run_rpc() { docker exec "$KERNEL" /app/bin/swarm rpc "$(cat "$WHO/$1")" </dev/null; }

{
  echo "== who refresh $STAMP =="
  echo "-- distill directory (host-side, allowlist) --"
  # ldap3 + pyyaml pulled on demand into an ephemeral uv env — no host-global install.
  uv run --quiet --with ldap3 --with pyyaml python "$WHO/ldap_who.py" --out "$FACTS"

  echo "-- load (atomic full replace) --"
  docker cp "$FACTS" "$KERNEL:/tmp/who_facts.json" >/dev/null
  run_rpc load_who.exs

  echo "== who refresh done $STAMP =="
} 2>&1 | tee -a "$LOG"

# tidy the transient facts (contains allowlisted PII — never leave it lying in tmp/)
rm -f "$FACTS"
