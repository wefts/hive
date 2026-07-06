#!/usr/bin/env python3
"""Deterministic IaC → network-fact extractor (network-map Phase-2; local, no LLM, no leak).

GENERIC by design (no intranet project names in this committed file): given a cloned repo DIR, it
tries every parser and keeps what matches by FILE PRESENCE (ipsec group_vars, ansible YAML
inventories, kubespray). Emits governed network facts (same vocabulary as
`Swarm.Enrichment.NetworkMap`) as JSON on stdout:
  {"origin":"iac:<repo>","reliability":0.85,"evidence_kind":"observation","facts":[{subject,
   subject_kind,relation,object,object_kind}, ...]}
Only DISTILLED facts leave here — never raw config. Well-typed by construction (passes the kernel's
relation↔kind signature).
"""
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    print(json.dumps({"error": "pyyaml missing"}))
    sys.exit(1)

ROOT = sys.argv[1] if len(sys.argv) > 1 else "."
CIDR = re.compile(r"^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$")
IP = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# ansible/kubespray GROUP + placeholder names that leak in as "hosts" — not real machines.
HOST_DENY = {
    "localhost", "node1", "node2", "node3", "node4", "node5", "calico_rr",
    "kube_control_plane", "kube_node", "kube-master", "etcd", "k8s_cluster",
    "all", "ungrouped", "bastion",
}
META_GROUPS = {"all", "archive", "sample", "ungrouped", "localhost", "skip_lists", "vagrant_test"}


def load_yaml(path):
    try:
        with open(path, "r", errors="ignore") as f:
            return yaml.safe_load(f)
    except Exception:
        return None


def norm_ctx(name):
    """Canonicalize a cluster/context name so `_` and `-` variants merge across repos."""
    return name.strip().lower().replace("_", "-")


def _is_host(name):
    n = (name or "").strip()
    if not n or IP.match(n):
        return False
    if n.lower() in HOST_DENY:
        return False
    if n.startswith("kube_") or n.endswith("_rr"):
        return False
    return True


def _yaml_hosts(doc):
    hosts = set()

    def walk(node):
        if isinstance(node, dict):
            h = node.get("hosts")
            if isinstance(h, dict):
                hosts.update(str(k).strip() for k in h.keys())
            for v in node.values():
                walk(v)

    walk(doc)
    return {h for h in hosts if _is_host(h)}


def parse_ipsec(repo_dir):
    """ipsec.yml: ipsec_tunnels[{name, remote, remote_subnets, separate_our_subnets}]."""
    facts = []
    for path in (
        os.path.join(repo_dir, "group_vars/all/ipsec.yml"),
        os.path.join(repo_dir, "specific-gateway.yml"),
    ):
        if not os.path.exists(path):
            continue
        doc = load_yaml(path)
        if not isinstance(doc, dict):
            continue
        for t in doc.get("ipsec_tunnels") or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name", "")).strip()
            if not name:
                continue
            remote = str(t.get("remote", "")).strip()
            if IP.match(remote):
                facts.append((name, "tunnel", "terminates_at", remote, "gateway"))
            for key in ("remote_subnets", "separate_our_subnets"):
                for sub in t.get(key, []) or []:
                    sub = str(sub).strip()
                    if CIDR.match(sub):
                        facts.append((name, "tunnel", "carries", sub, "subnet"))
    return facts


def parse_kubespray(repo_dir):
    """kubespray inventory: per-inventory k8s cluster + its pod/service CIDRs."""
    facts = []
    inv_root = os.path.join(repo_dir, "inventory")
    if not os.path.isdir(inv_root):
        return facts
    for inv in sorted(os.listdir(inv_root)):
        if inv in ("sample",):
            continue
        cluster = norm_ctx(inv)
        gv = os.path.join(inv_root, inv, "group_vars", "k8s_cluster", "k8s-cluster.yml")
        doc = load_yaml(gv) if os.path.exists(gv) else None
        if isinstance(doc, dict):
            for key in ("kube_pods_subnet", "kube_service_addresses"):
                val = str(doc.get(key, "")).strip()
                if CIDR.match(val):
                    facts.append((cluster, "cluster", "carries", val, "subnet"))
    return facts


def parse_ansible_inventory(repo_dir):
    """Ansible YAML inventories under inventory/<context>.yml → cluster/<context> contains host."""
    facts = []
    inv_dir = os.path.join(repo_dir, "inventory")
    if not os.path.isdir(inv_dir):
        return facts
    for f in os.listdir(inv_dir):
        if not f.endswith((".yml", ".yaml")):
            continue
        stem = os.path.splitext(f)[0]
        if stem in META_GROUPS:
            continue
        ctx = norm_ctx(stem)
        for h in _yaml_hosts(load_yaml(os.path.join(inv_dir, f))):
            facts.append((ctx, "cluster", "contains", h, "host"))
    return facts


PARSERS = [parse_ipsec, parse_kubespray, parse_ansible_inventory]


def main():
    repo = os.path.basename(ROOT.rstrip("/"))
    facts, seen = [], set()
    for p in PARSERS:
        for f in p(ROOT):
            if f not in seen:
                seen.add(f)
                facts.append(f)
    out = {
        "origin": f"iac:{repo}",
        "reliability": 0.85,
        "evidence_kind": "observation",
        "facts": [
            {"subject": s, "subject_kind": sk, "relation": r, "object": o, "object_kind": ok}
            for (s, sk, r, o, ok) in facts
        ],
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
