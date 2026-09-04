#!/usr/bin/env bash
# Snapshot the Proxmox API as the GRADER's ground truth for the learner eval.
#
# No model is involved: the API reports current state by construction, so a
# snapshot taken at instant T is the truth against which an inventory answer is
# graded. Output is a single JSON document per run, timestamped, and written
# under hive/tmp (gitignored) because it carries real host names.
#
# Read-only by construction: GET only, PVEAuditor token, TLS verified.
#
# Usage:  scripts/proxmox_truth_snapshot.sh [--site forge] [--out PATH]
#         (default: every site in PROXMOX_SITES)
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
sites_arg=""
out=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --site) sites_arg="${2:?}"; shift 2 ;;
    --out)  out="${2:?}"; shift 2 ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "proxmox_truth_snapshot: unknown argument: $1" >&2; exit 2 ;;
  esac
done

set -a
# shellcheck source=/dev/null
. "$here/secrets.env"
set +a

sites="${sites_arg:-${PROXMOX_SITES:?PROXMOX_SITES is not set}}"
out="${out:-$here/tmp/learner-eval/proxmox_truth_$(date -u +%Y%m%dT%H%M%SZ).json}"
observed="$(date -u +%FT%TZ)"

ca=()
[ -n "${PROXMOX_CA_PEM:-}" ] && [ -f "${PROXMOX_CA_PEM}" ] && ca=(--cacert "${PROXMOX_CA_PEM}")

api() { # site path -> raw body on stdout
  local s up id sec url
  s="$(echo "$1" | tr '[:lower:]' '[:upper:]')"
  up="PROXMOX_${s}_URL"; id="PROXMOX_${s}_TOKEN_ID"; sec="PROXMOX_${s}_TOKEN_SECRET"
  url="${!up:?missing $up}"
  case "$url" in *://*) ;; *) url="https://$url" ;; esac
  url="${url%/}"
  curl -fsS --max-time 30 "${ca[@]}" \
    -H "Authorization: PVEAPIToken=${!id:?missing $id}=${!sec:?missing $sec}" \
    "${url}$2"
}

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

for site in ${sites//,/ }; do
  api "$site" "/api2/json/cluster/status"            > "$tmp/$site.status.json"
  api "$site" "/api2/json/nodes"                     > "$tmp/$site.nodes.json"
  api "$site" "/api2/json/cluster/resources?type=vm" > "$tmp/$site.vms.json"
  echo "proxmox_truth_snapshot: $site ok" >&2
done

jq -n --arg observed "$observed" --arg sites "$sites" \
  '{observed_at:$observed, sites:($sites|split(",")|map(gsub("^\\s+|\\s+$";"")))}' > "$tmp/base.json"

python3 - "$tmp" "$out" "$observed" "$sites" <<'PY'
import json, sys, pathlib
tmp, out, observed, sites = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
tmp = pathlib.Path(tmp)
doc = {"observed_at": observed, "sites": {}}
for site in [s.strip() for s in sites.split(",") if s.strip()]:
    status = json.loads((tmp / f"{site}.status.json").read_text())["data"]
    nodes = json.loads((tmp / f"{site}.nodes.json").read_text())["data"]
    vms = json.loads((tmp / f"{site}.vms.json").read_text())["data"]
    cluster = next((e.get("name") for e in status if e.get("type") == "cluster"), site)
    doc["sites"][site] = {
        "cluster": cluster,
        "nodes": sorted(
            [{"node": n["node"], "status": n.get("status", "unknown")} for n in nodes if n.get("node")],
            key=lambda n: n["node"],
        ),
        "guests": sorted(
            [
                {
                    "vmid": v["vmid"],
                    "name": (v.get("name") or "").strip(),
                    "node": v["node"],
                    "status": v.get("status", "unknown"),
                    "type": v.get("type", ""),
                    "template": bool(v.get("template")),
                }
                for v in vms
                if isinstance(v.get("vmid"), int) and v.get("node")
            ],
            key=lambda v: v["vmid"],
        ),
    }
pathlib.Path(out).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
n_nodes = sum(len(s["nodes"]) for s in doc["sites"].values())
n_guests = sum(len(s["guests"]) for s in doc["sites"].values())
print(f"proxmox_truth_snapshot: wrote {out} sites={len(doc['sites'])} nodes={n_nodes} guests={n_guests}")
PY
