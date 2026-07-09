#!/usr/bin/env python3
"""Deterministic markdown-table extractor for the corpus (network-map Phase-2; local, no LLM).

Reads a bodies JSON ([{node_id, key, body}], produced by export_tables.exs) from argv[1] and emits
governed network facts as JSON on stdout:
  {"origin":"wiki:tables","reliability":0.55,"evidence_kind":"observation","facts":[...]}
Targets INVENTORY tables (a name/host/fqdn column + IP column(s)) → `host has_address address <ip>`.
Entity name prefers the FQDN (cut at its first TLD, so concatenated cells don't merge). Only
distilled facts leave here; conservative (skip tables with no clear entity or IP column).
"""
import json
import re
import sys

IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
FIRST_FQDN = re.compile(r"[a-z0-9][a-z0-9.-]*?\.(?:intranet|fr|com|net|org|local|lan)", re.I)
ENTITY_HDR = re.compile(r"\b(host\s*name|hostname|host|role|server|machine|node|vm|name|fqdn|dns)\b", re.I)
FQDN_HDR = re.compile(r"\b(fqdn|dns|domain)\b", re.I)
IP_HDR = re.compile(r"\b(ip|address|adresse|wan|lan)\b", re.I)
MAC_HDR = re.compile(r"\bmac\b", re.I)

BODIES = sys.argv[1] if len(sys.argv) > 1 else "/tmp/wiki_bodies.json"


def first_fqdn(s):
    m = FIRST_FQDN.search(s or "")
    return m.group(0).lower() if m else ""


def clean_cell(s):
    s = (s or "").strip()
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[*`]", "", s)
    return s.strip()


def clean_entity(s):
    s = clean_cell(s)
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"^\d+\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if re.fullmatch(r"[\d\s]*", s) or len(s) < 2:
        return ""
    return s


def is_sep(cells):
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", (c or "").strip() or "") for c in cells)


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def find_tables(body):
    lines = body.split("\n")
    i = 0
    while i < len(lines):
        if lines[i].count("|") >= 2 and i + 1 < len(lines) and is_sep(split_row(lines[i + 1])):
            header = split_row(lines[i])
            rows, j = [], i + 2
            while j < len(lines) and lines[j].count("|") >= 2:
                cells = split_row(lines[j])
                if not is_sep(cells):
                    rows.append(cells)
                j += 1
            if rows:
                yield header, rows
            i = j
            continue
        i += 1


def extract(body):
    facts = []
    for header, rows in find_tables(body):
        cols = [clean_cell(h) for h in header]
        ent_idx = fqdn_idx = None
        ip_idxs = []
        for idx, h in enumerate(cols):
            if MAC_HDR.search(h):
                continue
            if fqdn_idx is None and FQDN_HDR.search(h):
                fqdn_idx = idx
            if ent_idx is None and ENTITY_HDR.search(h):
                ent_idx = idx
            if IP_HDR.search(h):
                ip_idxs.append(idx)
        if (ent_idx is None and fqdn_idx is None) or not ip_idxs:
            continue
        for r in rows:
            def cell(idx):
                return r[idx] if idx is not None and idx < len(r) else ""

            fq = first_fqdn(clean_cell(cell(fqdn_idx))) if fqdn_idx is not None else ""
            ent = fq or first_fqdn(clean_cell(cell(ent_idx))) or clean_entity(cell(ent_idx)) if ent_idx is not None else fq
            ent = (ent or "").lower().strip()
            if not ent or len(ent) < 2 or IP.fullmatch(ent):
                continue
            for ii in ip_idxs:
                for ip in IP.findall(clean_cell(cell(ii))):
                    facts.append((ent, "host", "has_address", ip, "address"))
    return facts


def main():
    data = json.load(open(BODIES))
    # S1: per-fact lineage = the source PAGE node (`wiki:page:<node>`), so a table fact from page A
    # and the same fact from page B are 2 independent votes, while all passes over page A share one.
    seen, facts = set(), []
    for page in data:
        node = page.get("node_id")
        lineage = "wiki:page:%s" % node if node is not None else None
        for f in extract(page.get("body") or ""):
            key = (f, lineage)
            if key not in seen:
                seen.add(key)
                facts.append((f, lineage))
    out = {
        "origin": "wiki:tables",
        "reliability": 0.55,
        "evidence_kind": "observation",
        "facts": [
            {"subject": s, "subject_kind": sk, "relation": r, "object": o, "object_kind": ok, "lineage": lin}
            for ((s, sk, r, o, ok), lin) in facts
        ],
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
