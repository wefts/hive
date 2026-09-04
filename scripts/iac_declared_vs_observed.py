#!/usr/bin/env python3
"""Declared (Ansible inventories) versus observed (Proxmox snapshot). COMPARISON, NOT INGEST.

`board/todo/declared-versus-observed.md`: ingesting inventory into the graph would
collide with the IaC source already there, and two competing host lists are worse than
one incomplete list. So the first use is a diff, and who is authoritative gets written
down before either is loaded.

Authority, per `board/todo/source-authority.md` as corrected:

  * authority is **conditional on the observation**, not unconditional precedence.
    Proxmox is where placement is decided, so it defines placement facts -- but only for
    a run that COMPLETED. A partial API response must never become "the VM is gone".
  * a declared-but-absent host is **stale declaration**, not a corroboration conflict.
    Averaging the two away destroys the signal the pair exists to produce.
  * absence is authoritative only under a complete snapshot, which is why this refuses
    to classify drift at all unless every configured site is present in the snapshot.

The clone is a snapshot that goes stale in silence, so the inventory commit SHA of every
repo read is recorded next to the condition hash. A drift report against a three-week-old
clone is a report about the clone.

Reads only. Writes nothing to the graph, sends nothing outbound. Output carries real host
names -> hive/tmp, gitignored.
"""
import argparse
import collections
import datetime
import hashlib
import json
import pathlib
import re
import subprocess
import sys

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("iac_declared_vs_observed: pyyaml is required")

HOST_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.(intranet|local|internal)$|^[a-z0-9][a-z0-9-]{2,}$", re.I)

# Group names that describe a FLEET or an environment, not a service. A fleet group says
# nothing about what a host runs, so it is never a service->host declaration.
FLEET_RE = re.compile(
    r"^(all|ungrouped|archive|.*_nodes|.*_hosts|.*_vms|win_vms|bare_metal|"
    r"galaxy(_dev|_pp|_prod)?(_core)?|forge.*|casa.*|idf.*|mpl.*|applications|"
    r"firewalls.*|agency_asn|bastions|.*_runners)$",
    re.I,
)


def repo_shas(root):
    out = {}
    for d in sorted(p for p in pathlib.Path(root).iterdir() if (p / ".git").exists()):
        try:
            sha = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"],
                                 capture_output=True, text=True, check=True).stdout.strip()
            when = subprocess.run(["git", "-C", str(d), "log", "-1", "--format=%cI"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            dirty = subprocess.run(["git", "-C", str(d), "status", "--porcelain"],
                                   capture_output=True, text=True, check=True).stdout.strip()
            out[d.name] = {"sha": sha, "committed_at": when, "dirty": bool(dirty)}
        except subprocess.CalledProcessError:
            out[d.name] = {"sha": "unknown", "committed_at": "", "dirty": True}
    return out


def read_groups(root, skip_dynamic=True):
    """service/fleet group -> hosts, from static inventories only.

    A dynamic plugin file (`*.proxmox.yml`) is skipped: it is generated FROM the Proxmox
    API, so consuming it would be the observed source wearing a declaration's hat and
    every agreement would be circular.
    """
    groups, files, skipped = collections.defaultdict(set), [], []

    def walk(node, name):
        if not isinstance(node, dict):
            return
        hosts = node.get("hosts")
        if isinstance(hosts, dict) and name:
            for h in hosts:
                if h and HOST_RE.match(str(h)):
                    groups[name].add(str(h))
        for k, v in (node.get("children") or {}).items():
            walk(v or {}, k)

    root = pathlib.Path(root)
    for f in sorted(set(list(root.rglob("inventory/*.y*ml")) + list(root.rglob("inventories/**/*.y*ml"))
                        + list(root.rglob("hosts.yml")))):
        if skip_dynamic and (".proxmox." in f.name or "plugin" in f.name):
            skipped.append(str(f.relative_to(root)))
            continue
        try:
            doc = yaml.safe_load(f.read_text())
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        files.append(str(f.relative_to(root)))
        for top, node in doc.items():
            walk(node or {}, top)
    return groups, files, skipped


# A declared host belongs to a site; a snapshot covers some sites. Absence is only
# meaningful where the two overlap -- an `idf` host missing from a forge+galaxy snapshot
# is out of scope, not drift. Getting this wrong turns "we did not look" into "it is gone",
# which is the exact failure `board/todo/source-authority.md` warns about.
SITE_SUFFIX = re.compile(r"\.([a-z0-9-]+)\.(intranet|local|internal)$", re.I)


def declared_site(host, forge_names):
    m = SITE_SUFFIX.search(host)
    if m:
        return m.group(1).lower()
    # Bare names are the forge convention; only claim it when the snapshot actually
    # has a forge host by that name, otherwise the site is unknown.
    return "forge" if host in forge_names else None


def observed(truth_path):
    doc = json.loads(pathlib.Path(truth_path).read_text())
    guests, nodes, placement = set(), set(), {}
    for site, s in doc["sites"].items():
        for n in s["nodes"]:
            nodes.add(n["node"])
        for g in s["guests"]:
            if g["name"] and not g["template"]:
                guests.add(g["name"])
                placement[g["name"]] = {"site": site, "node": g["node"], "status": g["status"]}
    return doc, guests, nodes, placement


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", required=True, help="clone root, e.g. ~/SmileRepos")
    ap.add_argument("--truth", required=True, help="proxmox truth snapshot")
    ap.add_argument("--sites", default="forge,galaxy", help="sites the snapshot covers")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    root = pathlib.Path(a.repos).expanduser()
    shas = repo_shas(root)
    groups, files, skipped = read_groups(root)
    truth, guests, nodes, placement = observed(a.truth)
    sites = [s.strip() for s in a.sites.split(",") if s.strip()]

    # Absence is authoritative only under a COMPLETE snapshot. If the snapshot does not
    # cover a site, a host declared there is unclassifiable, not missing.
    snapshot_sites = set(truth["sites"])
    complete = set(sites) <= snapshot_sites

    declared = set().union(*groups.values()) if groups else set()
    live = guests | nodes
    forge_names = {n for n in live if "." not in n}

    service_groups = {g: hs for g, hs in groups.items() if not FLEET_RE.match(g) and 1 <= len(hs) <= 3}
    fleet_groups = {g: hs for g, hs in groups.items() if g not in service_groups}

    # Partition the declared set by whether this snapshot can speak about it at all.
    in_scope, out_of_scope, unknown_site = set(), collections.defaultdict(set), set()
    for h in declared:
        site = declared_site(h, forge_names)
        if site is None:
            unknown_site.add(h)
        elif site in snapshot_sites:
            in_scope.add(h)
        else:
            out_of_scope[site].add(h)

    declared_live = sorted(in_scope & live)
    declared_absent = sorted(in_scope - live)
    observed_undeclared = sorted(live - declared)

    archive = groups.get("archive", set())

    report = {
        "kind": "iac_declared_vs_observed",
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "snapshot_observed_at": truth["observed_at"],
        "snapshot_sites": sorted(snapshot_sites),
        "snapshot_complete_for_requested_sites": complete,
        "inventory_repos": shas,
        "inventory_files_read": files,
        "inventory_files_skipped_as_dynamic": skipped,
        "counts": {
            "groups": len(groups),
            "service_shaped_groups": len(service_groups),
            "fleet_groups": len(fleet_groups),
            "declared_hosts": len(declared),
            "declared_in_snapshot_scope": len(in_scope),
            "declared_out_of_snapshot_scope": {s: len(v) for s, v in sorted(out_of_scope.items())},
            "declared_site_unknown": len(unknown_site),
            "observed_hosts": len(live),
            "declared_and_observed": len(declared_live),
            "declared_not_observed": len(declared_absent),
            "observed_not_declared": len(observed_undeclared),
            "archive_declared": len(archive),
            "archive_still_observed": len(archive & live),
        },
        "declaration_accuracy": {
            "_what_this_is": (
                "the check that the source says what we think, on the dimension BOTH sources "
                "speak to (existence), independent of the service-grouping rule that would "
                "consume it. A source accurate here is worth consuming on the dimension only "
                "it speaks to; that step is an inference and is stated as one."
            ),
            "declared_hosts_that_exist": (
                round(len(declared_live) / len(in_scope), 3) if in_scope else None),
            "_denominator": ("hosts declared at a site THIS snapshot covers; a host at an "
                             "uncovered site is out of scope, never drift"),
            "archive_hosts_that_are_gone": round(1 - len(archive & live) / len(archive), 3) if archive else None,
        },
        "drift": {
            "_authoritative": complete,
            "_note": ("stale declaration, not a corroboration conflict: Proxmox defines placement "
                      "for a completed run, so a declared host it does not report is a declaration "
                      "that has gone stale — unless the snapshot is partial, in which case nothing "
                      "here is classifiable"),
            "declared_not_observed": declared_absent,
            "observed_not_declared_sample": observed_undeclared[:40],
        },
        "service_declarations": {g: sorted(hs) for g, hs in sorted(service_groups.items())},
    }

    payload = json.dumps({k: v for k, v in report.items() if k != "generated_at"},
                         sort_keys=True).encode()
    report["condition_hash"] = hashlib.sha256(payload).hexdigest()

    pathlib.Path(a.out).write_text(json.dumps(report, indent=2) + "\n")

    c = report["counts"]
    print(f"declared-vs-observed  snapshot={truth['observed_at']} complete={complete}")
    print(f"  inventory: {', '.join(f'{k}@{v['sha'][:7]}' for k, v in shas.items())}")
    print(f"  files read {len(files)}, skipped as dynamic {len(skipped)}: {skipped}")
    print(f"  groups {c['groups']} ({c['service_shaped_groups']} service-shaped, "
          f"{c['fleet_groups']} fleet)")
    print(f"  declared {c['declared_hosts']} total, {c['declared_in_snapshot_scope']} in snapshot scope "
          f"(out of scope: {c['declared_out_of_snapshot_scope']}, site unknown: {c['declared_site_unknown']})")
    print(f"  observed {c['observed_hosts']} · declared AND observed {c['declared_and_observed']}")
    print(f"  DRIFT declared-not-observed {c['declared_not_observed']} · "
          f"observed-not-declared {c['observed_not_declared']}")
    print(f"  archive declared {c['archive_declared']}, still observed {c['archive_still_observed']}")
    da = report["declaration_accuracy"]
    print(f"  declaration accuracy: {da['declared_hosts_that_exist']} of declared hosts exist; "
          f"{da['archive_hosts_that_are_gone']} of archived hosts are gone")
    print(f"  condition_hash {report['condition_hash']}")
    print(f"declared-vs-observed: wrote {a.out}")


if __name__ == "__main__":
    main()
