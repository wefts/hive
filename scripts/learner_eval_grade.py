#!/usr/bin/env python3
"""Grade a learner-eval run against the Proxmox API snapshot. No model involved.

The classification rules are PRE-REGISTERED in
`hive/docs/design/learner-eval-grading.md` and are not to be changed while grading.
This file implements that document and nothing else; the summary it writes carries
`rules_version` and a SHA-256 of the document, and the run aborts if the two
disagree. A rule invented after seeing the row it catches is a hypothesis fitted to
its own data -- that is what happened to `wrong_subject` in v1.

What changed in v2, and why every v1 number is unsafe as a join measure: v1 scored a
`placement` row correct on the Proxmox node alone, requiring no document evidence at
all, so a pure inventory lookup raised the number called "join rate". v2 requires
evidence from BOTH sources about the SAME subject, and gives the inventory-only case
its own class instead of silently counting it as a join.

Usage:
  scripts/learner_eval_grade.py --run ... --truth ... --overlap ... \
      --docs-with-hosts ... --out ... [--rules-doc docs/design/learner-eval-grading.md]
"""
import argparse
import collections
import csv
import datetime
import hashlib
import json
import pathlib
import re
import sys

RULES_VERSION = 2

# Evidence-bearing classes, in the order the pre-registration assigns them.
CLASSES = ("no_answer", "wrong", "wrong_subject", "answered_off",
           "corpus_only", "inventory_only", "join_correct")


# --- inputs ------------------------------------------------------------------------


def load_rules(path):
    """The pre-registration is part of the measurement: hash it, check its version."""
    doc = pathlib.Path(path)
    if not doc.exists():
        sys.exit(f"learner_eval_grade: pre-registration {path} not found; refusing to grade")
    text = doc.read_text()
    m = re.search(r"^rules_version:\s*(\d+)\s*$", text, re.MULTILINE)
    if not m:
        sys.exit(f"learner_eval_grade: {path} has no `rules_version:` in its frontmatter")
    if int(m.group(1)) != RULES_VERSION:
        sys.exit(f"learner_eval_grade: {path} declares rules_version {m.group(1)}, "
                 f"this grader implements {RULES_VERSION}")
    return {"rules_version": RULES_VERSION, "rules_doc": str(path),
            "rules_sha256": hashlib.sha256(text.encode()).hexdigest()}


def load_truth(path):
    doc = json.loads(pathlib.Path(path).read_text())
    nodes, guests = collections.defaultdict(list), collections.defaultdict(dict)
    for site, s in doc["sites"].items():
        nodes[site] = [n["node"] for n in s["nodes"]]
        for g in s["guests"]:
            if g["name"] and not g["template"]:
                guests[site][g["name"].lower()] = g
    return doc, nodes, guests


# --- matching mechanics ------------------------------------------------------------


def mentions(text, name):
    """Word-boundary, case-insensitive, `-` and `_` part of the name."""
    if not name:
        return False
    return re.search(r"(^|[^A-Za-z0-9_-])" + re.escape(name) + r"([^A-Za-z0-9_-]|$)",
                     text or "", re.IGNORECASE) is not None


def mentions_host(text, host):
    """A host is mentioned by its full name or by its FQDN stem, as a whole token."""
    return mentions(text, host) or mentions(text, (host or "").split(".")[0])


def nodes_named(text, site_node_names):
    return [n for n in site_node_names if mentions(text, n)]


def document_evidence(citations, host, docs_by_host):
    """Citations that resolve to a document the CORPUS really shows this host in.

    Verified against docs_with_hosts.csv, never against the answer's own prose.
    Core.ask cites by document title, so titles and source_refs are both indexed.
    """
    refs = set()
    for c in citations or []:
        for key in ("ref", "url", "source"):
            v = c.get(key)
            if isinstance(v, str) and v:
                refs.add(v.strip().lower())
    hits = []
    for source_ref, title in docs_by_host.get((host or "").lower(), ()):
        keys = {source_ref.lower(), title.strip().lower(), source_ref.split(":")[-1].lower()}
        if refs & keys or any(k and k in r for r in refs for k in keys if len(k) > 6):
            hits.append(source_ref)
    return sorted(set(hits))


# --- the rules ---------------------------------------------------------------------


def classify(inv, doc, status):
    """The final assignment once contradictions have been ruled out."""
    if status != "found":
        return "no_answer"
    if inv and doc:
        return "join_correct"
    if inv:
        return "inventory_only"
    if doc:
        return "corpus_only"
    return "answered_off"


def grade_row(row, site_nodes, site_guests, doc_mentions, docs_by_host):
    shape, site = row["shape"], row.get("site")
    expect = row.get("expect") or {}
    answer = row.get("swarm_answer") or ""
    status = row.get("swarm_status")
    out = {"grader": "proxmox_api", "rules_version": RULES_VERSION}

    if shape == "undocumented":
        out.update(grade_undocumented(status, answer, site, site_guests, doc_mentions))
        return out

    if shape == "accuracy" and expect.get("kind") == "page_still_true":
        out.update(grade_page_accuracy(row, status, answer, expect, site_nodes, docs_by_host))
        return out

    host = expect.get("host") or ""
    want = expect.get("node")
    site_node_names = site_nodes.get(site, [])
    named = nodes_named(answer, site_node_names)
    doc = document_evidence(row.get("citations"), host, docs_by_host)
    others = [g["name"] for g in site_guests.get(site, {}).values()
              if g["name"].lower() != host.lower() and mentions_host(answer, g["name"])]

    out.update({"nodes_named": named, "expected_node": want,
                "cited_docs_mentioning_host": doc, "other_hosts_named": others})

    verdict = placement_verdict(status, answer, host, want, named, others)
    out.update(verdict)
    if verdict.get("class"):
        return out

    out["class"] = classify(verdict["inventory_evidence"], bool(doc), status)
    if out["class"] == "answered_off":
        out["why"] = "answered with neither an inventory fact nor a citation reaching this host"
    elif out["class"] == "inventory_only":
        out["why"] = "correct placement, but no citation reaches a document mentioning this host"
    elif out["class"] == "corpus_only":
        out["why"] = "correct from documents; nothing in the answer comes from the inventory"
    return out


def placement_verdict(status, answer, host, want, named, others):
    """Contradiction and subject checks shared by placement, purpose and host accuracy."""
    if status != "found":
        return {"class": "no_answer", "inventory_evidence": False}

    wrong_nodes = [n for n in named if n != want]
    subject_named = mentions_host(answer, host)

    if wrong_nodes and not subject_named and others:
        return {"class": "wrong_subject", "inventory_evidence": False,
                "why": f"answers about {others}, not {host}"}
    if wrong_nodes:
        return {"class": "wrong", "inventory_evidence": False,
                "why": f"places {host} on {wrong_nodes}, API says {want}"}
    if want in named and not subject_named and others:
        return {"class": "wrong_subject", "inventory_evidence": False,
                "why": f"names the right node but answers about {others}, not {host}"}

    return {"inventory_evidence": want in named}


def grade_page_accuracy(row, status, answer, expect, site_nodes, docs_by_host):
    page = expect.get("hosts", [])
    discussed = [h for h in page if mentions_host(answer, h["host"])]
    allowed = {h["node"] for h in (discussed or page)}
    all_nodes = sorted({n for s in site_nodes for n in site_nodes[s]})
    named = nodes_named(answer, all_nodes)
    wrong_nodes = [n for n in named if n not in allowed]

    doc = []
    for h in (discussed or page):
        doc += document_evidence(row.get("citations"), h["host"], docs_by_host)
    doc = sorted(set(doc))

    out = {"hosts_discussed": [h["host"] for h in discussed], "nodes_named": named,
           "cited_docs_mentioning_host": doc}
    if status != "found":
        out["class"] = "no_answer"
    elif wrong_nodes:
        out["class"] = "wrong"
        out["why"] = f"places page hosts on {wrong_nodes}, API says {sorted(allowed)}"
    else:
        out["inventory_evidence"] = bool(named)
        out["class"] = classify(bool(named), bool(doc), status)
        if out["class"] == "answered_off":
            out["why"] = "answer states no node for any host this page names"
    return out


def grade_undocumented(status, answer, site, site_guests, doc_mentions):
    """Reported separately: its evidence is inventory plus the ABSENCE of documents,
    so document evidence cannot be positive by construction."""
    guests = site_guests.get(site, {})
    named = sorted(g["name"] for g in guests.values() if mentions_host(answer, g["name"]))
    documented = [n for n in named if doc_mentions.get((site, n.lower()), 0) > 0]
    out = {"hosts_named": named, "documented_but_named": documented,
           "truly_undocumented": [n for n in named if n not in documented]}
    if status != "found":
        out["class"] = "no_answer"
    elif not named:
        out["class"] = "answered_off"
        out["why"] = "answer names no host from this site"
    elif documented:
        out["class"] = "wrong"
        out["why"] = f"names hosts that ARE documented: {documented}"
    else:
        out["class"] = "correct_undocumented"
    return out


# --- reporting ---------------------------------------------------------------------


def summarize(graded, header, rules):
    def counts(rows, key="class"):
        return dict(collections.Counter(r[key] for r in rows))

    joins = [r for r in graded if r.get("join") and r["shape"] != "undocumented"]
    singles = [r for r in graded if not r.get("join")]
    undoc = [r for r in graded if r["shape"] == "undocumented"]
    controls = [r for r in graded if r.get("control_class")]

    def n(rows, *classes):
        return sum(1 for r in rows if r["class"] in classes)

    def by(rows, field):
        out = {}
        for value in sorted({(r.get(field) or "n/a") for r in rows}):
            sub = [r for r in rows if (r.get(field) or "n/a") == value]
            out[value] = {"n": len(sub), "join_correct": n(sub, "join_correct"),
                          "counts": counts(sub)}
        return out

    # A control succeeds by reaching the inventory; no join is being asked of it.
    ctrl_ok = sum(1 for r in controls if r["control_class"] in ("inventory_only", "join_correct"))
    full_name = [r for r in controls if r.get("control_names_full_subject")]
    part_name = [r for r in controls if not r.get("control_names_full_subject")]

    return {
        "kind": "learner_eval_grade",
        "graded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "grader": "proxmox_api",
        **rules,
        "run": {k: header.get(k) for k in ("measured_at", "condition_hash", "rows")},
        "set": header.get("set"),
        "headline": {
            "join_questions": len(joins),
            "join_correct": n(joins, "join_correct"),
            "join_rate": round(n(joins, "join_correct") / len(joins), 3) if joins else None,
            "inventory_only": n(joins, "inventory_only"),
            "corpus_only": n(joins, "corpus_only"),
            "single_source_questions": len(singles),
            "single_source_reached_inventory": n(singles, "inventory_only", "join_correct"),
            "control_pairs": len(controls),
            "control_reached_inventory": ctrl_ok,
            "control_rate": round(ctrl_ok / len(controls), 3) if controls else None,
            "control_full_subject_name": {
                "n": len(full_name),
                "reached_inventory": sum(1 for r in full_name if r["control_class"]
                                         in ("inventory_only", "join_correct")),
            },
            "control_partial_subject_name": {
                "n": len(part_name),
                "reached_inventory": sum(1 for r in part_name if r["control_class"]
                                         in ("inventory_only", "join_correct")),
            },
            "undocumented_questions": len(undoc),
            "undocumented_correct": n(undoc, "correct_undocumented"),
        },
        "counts": counts(graded),
        "by_shape": by(graded, "shape"),
        "by_name_identity": by(graded, "name_identity"),
        "by_shape_within_name_identity": {
            ident: dict(collections.Counter(
                r["shape"] for r in graded if (r.get("name_identity") or "n/a") == ident))
            for ident in sorted({(r.get("name_identity") or "n/a") for r in graded})
        },
    }


def report(s, graded):
    h = s["headline"]
    print(f"learner-eval GRADE  rules_v{s['rules_version']} "
          f"sha={s['rules_sha256'][:12]}  set={(s.get('set') or {}).get('label')} "
          f"set_hash={(s.get('set') or {}).get('set_hash', '')[:16]} "
          f"condition_hash={(s.get('run') or {}).get('condition_hash', '')[:16]}")
    print(f"  JOIN (both sources, same subject)  {h['join_correct']}/{h['join_questions']}"
          f"  rate={h['join_rate']}")
    print(f"    inventory_only (v1 counted these as joins)  {h['inventory_only']}")
    print(f"    corpus_only                                 {h['corpus_only']}")
    print(f"  SINGLE-SOURCE reached inventory    "
          f"{h['single_source_reached_inventory']}/{h['single_source_questions']}")
    print(f"  CONTROL reached inventory          "
          f"{h['control_reached_inventory']}/{h['control_pairs']}  rate={h['control_rate']}")
    print(f"    control names full subject       "
          f"{h['control_full_subject_name']['reached_inventory']}"
          f"/{h['control_full_subject_name']['n']}")
    print(f"    control names partial subject    "
          f"{h['control_partial_subject_name']['reached_inventory']}"
          f"/{h['control_partial_subject_name']['n']}")
    print(f"  UNDOCUMENTED (reported apart)      "
          f"{h['undocumented_correct']}/{h['undocumented_questions']}")
    print(f"  all classes {s['counts']}")
    for shape, v in s["by_shape"].items():
        print(f"    {shape:14} n={v['n']:3} join_correct={v['join_correct']} {v['counts']}")
    print("  by how the question named the host (with its shape composition):")
    for ident, v in s["by_name_identity"].items():
        comp = s["by_shape_within_name_identity"].get(ident, {})
        print(f"    {ident:9} n={v['n']:3} join_correct={v['join_correct']} {v['counts']}")
        print(f"    {'':9}     composition={comp}")
    failures = collections.Counter(
        (r["shape"], r["class"], (r.get("why") or "")[:58])
        for r in graded if r["class"] not in ("join_correct", "correct_undocumented"))
    if failures:
        print("  failure shapes:")
        for (shape, cls, why), k in failures.most_common():
            print(f"    {k:3}x {shape}/{cls} {why}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--truth", required=True)
    ap.add_argument("--overlap", required=True)
    ap.add_argument("--docs-with-hosts", required=True)
    ap.add_argument("--rules-doc",
                    default=str(pathlib.Path(__file__).resolve().parent.parent
                                / "docs" / "design" / "learner-eval-grading.md"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rules = load_rules(a.rules_doc)
    truth, site_nodes, site_guests = load_truth(a.truth)

    doc_mentions = {}
    for r in csv.DictReader(open(a.overlap)):
        doc_mentions[(r["site"], r["name"].lower())] = int(r["conf_docs"]) + int(r["wiki_docs"])

    docs_by_host = collections.defaultdict(set)
    for r in csv.DictReader(open(a.docs_with_hosts)):
        for entry in r["hostlist"].split():
            _, _, name = entry.partition("/")
            docs_by_host[name.lower()].add((r["source_ref"], r["title"]))

    lines = [json.loads(l) for l in open(a.run) if l.strip()]
    header, rows = lines[0], lines[1:]

    graded = []
    for row in rows:
        g = grade_row(row, site_nodes, site_guests, doc_mentions, docs_by_host)

        if row.get("control_question"):
            expect = row.get("expect") or {}
            host = expect.get("host") or ""
            ctrl = {**row, "swarm_answer": row.get("control_answer"),
                    "swarm_status": row.get("control_status"),
                    "citations": row.get("control_citations")}
            cg = grade_row(ctrl, site_nodes, site_guests, doc_mentions, docs_by_host)
            g["control_class"] = cg["class"]
            g["control_nodes_named"] = cg.get("nodes_named")
            # v1 read control failures as an unreachable inventory; most controls do not
            # even contain the full subject name, so resolution was never excluded.
            g["control_names_full_subject"] = mentions(row["control_question"], host)

        graded.append({**row, **g})

    summary = summarize(graded, header, rules)
    with open(a.out, "w") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        for g in graded:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")

    report(summary, graded)
    print(f"learner_eval_grade: wrote {a.out}")


if __name__ == "__main__":
    main()
