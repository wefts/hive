#!/usr/bin/env python3
"""Grade a learner-eval run against the Proxmox API snapshot. No model involved.

The classification rules are PRE-REGISTERED in
`hive/docs/design/learner-eval-grading.md` and validated by
`learner_eval_validate_grader.py` against synthetic positive and adversarial fixtures.
Run the validator before trusting any number this produces: hashing and pre-registration
make a classifier reproducible, not correct, and v2 scored 5 of 13 fixtures wrong --
three of them by calling a fake a join.

THIS IS A CONCORDANCE METRIC, NOT A PROVENANCE ONE, and the output says so. Only the
`structured` case is provenance in the strict sense -- a citation naming the graph key
the serve path actually read. `exclusive` is an inference from absence, and document
evidence is an inference from a citation plus answer length: fixture
`L1-concordance-ceiling` is a counterexample that passes both. No analysis of answer
text can separate a join from a coincidence; the fix is a per-answer provenance record
emitted by Swarm.Core. Until then, report `concordance`.

  inventory evidence  a structured citation for the subject's own graph key (provenance),
                      or the node named where no CITED document contains it (inference)
  subject binding     required before inventory evidence counts at all
  document evidence   a cited document that really contains the host, plus a substantive
                      answer (an incidental citation on a one-word answer is not evidence)
  pairing             per host, never accumulated across a page's host set

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

RULES_VERSION = 5
MIN_CONTENT_TOKENS = 3

STOPWORDS = set("""
a an and are as at be been by can do does for from had has have how in into is it its
of on or that the their there they this to was were what when where which who why with
your you not no yes still today currently run runs running host hosts hosted hosting
node nodes hypervisor hypervisors proxmox machine machines vm vms server servers service
services page pages according documentation docs doc sur les des est une un le la pour
dans avec que qui sont être plus toujours
""".split())


# --- inputs ------------------------------------------------------------------------


def load_rules(path):
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
    # The rules doc hash binds PROSE to output. The grader's own source is the thing that
    # actually classifies, so hash it too: without this, two implementations of the same
    # document share one hash.
    impl = pathlib.Path(__file__).read_bytes()
    return {"rules_version": RULES_VERSION, "rules_doc": str(path),
            "rules_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "grader_sha256": hashlib.sha256(impl).hexdigest()}


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
    if not name:
        return False
    return re.search(r"(^|[^A-Za-z0-9_-])" + re.escape(name) + r"([^A-Za-z0-9_-]|$)",
                     text or "", re.IGNORECASE) is not None


def mentions_host(text, host):
    return mentions(text, host) or mentions(text, (host or "").split(".")[0])


def nodes_named(text, site_node_names):
    return [n for n in site_node_names if mentions(text, n)]


def tokens(text):
    return {t for t in re.split(r"[^A-Za-z0-9]+", (text or "").lower()) if t}


def structured_refs(citations):
    return {(c.get("ref") or "").strip()
            for c in citations or [] if (c.get("source") or "") == "structured"}


def cited_keys(citations):
    out = set()
    for c in citations or []:
        for key in ("ref", "url", "source"):
            v = c.get(key)
            if isinstance(v, str) and v:
                out.add(v.strip().lower())
    return out


def cited_docs_containing(citations, name, docs_by_host):
    """Cited documents that the CORPUS shows contain this name (host or node).

    Verified against docs_with_hosts.csv, never against the answer's prose. Core.ask
    cites by document title, so titles and source_refs are both indexed.
    """
    refs = cited_keys(citations)
    hits = []
    for source_ref, title in docs_by_host.get((name or "").lower(), ()):
        keys = {source_ref.lower(), title.strip().lower(), source_ref.split(":")[-1].lower()}
        if refs & keys or any(k and k in r for r in refs for k in keys if len(k) > 6):
            hits.append(source_ref)
    return sorted(set(hits))


def substantive(answer, question, identifier_tokens):
    """Enough content of its own that an attached citation is not merely incidental."""
    content = tokens(answer) - tokens(question) - STOPWORDS - identifier_tokens
    content = {t for t in content if len(t) > 2 and not t.isdigit()}
    return len(content) >= MIN_CONTENT_TOKENS, sorted(content)[:12]


def identifier_tokens_for(site, site_nodes, site_guests):
    ids = set()
    for name in list(site_nodes.get(site, [])) + [g["name"] for g in site_guests.get(site, {}).values()]:
        ids |= tokens(name)
    ids |= tokens(site)
    return ids


# --- provenance: what the kernel says the answer was built from ----------------------


def provenance_evidence(record, site, host, node, docs_by_host):
    """Grade from the kernel's own record of what it read. No prose is consulted.

    `structured`: the serve path names the subject key it read and the facts it rendered.
    `consilium`: the grounded facts and the passages that entered the prompt.
    Returns None when there is no usable record, so the caller falls back to concordance.
    """
    if not isinstance(record, dict) or not record.get("kind"):
        return None

    key = f"net:host:{site}/{host}"
    facts = [f for f in record.get("facts") or [] if isinstance(f, dict)]
    subject_key = record.get("subject_key")

    def about_subject(f):
        if subject_key:
            return subject_key == key
        subj = str(f.get("subject") or "")
        return subj.lower() in {host.lower(), host.split(".")[0].lower()}

    inventory = any(
        about_subject(f) and str(f.get("object") or "").lower() == str(node or "").lower()
        for f in facts
    )

    passages = [p for p in record.get("passages") or [] if isinstance(p, dict)]
    host_docs = {ref for ref, _title in docs_by_host.get((host or "").lower(), ())}
    document = any(
        (p.get("source_ref") in host_docs)
        or any((p.get("key") or "").strip().lower() == title.strip().lower()
               for _ref, title in docs_by_host.get((host or "").lower(), ()))
        for p in passages
    )

    return {
        "evidence_basis": "provenance",
        "record_kind": record.get("kind"),
        "record_subject_key": subject_key,
        "inventory_evidence": "record" if inventory else None,
        "document_evidence": document,
        "grounded_fact_count": len(facts),
        "grounded_passage_count": len(passages),
    }


# --- evidence per host -------------------------------------------------------------


def host_evidence(answer, question, citations, site, host, node, docs_by_host, id_tokens,
                  page_row=False):
    key = f"net:host:{site}/{host}"
    structured = key in structured_refs(citations)
    named = mentions_host(answer, host)
    node_named = bool(node) and mentions(answer, node)
    node_in_cited = bool(cited_docs_containing(citations, node, docs_by_host))

    # On a page row the PAGE is the subject, so naming the host's node counts as
    # discussing it; on a host row the answer must say what it is talking about.
    bound = structured or named or (page_row and node_named)

    inventory = None
    if node_named and structured:
        inventory = "structured"
    elif node_named and bound and not node_in_cited:
        inventory = "exclusive"

    doc_hits = cited_docs_containing(citations, host, docs_by_host)
    subst, content = substantive(answer, question, id_tokens)
    document = bool(doc_hits) and subst

    return {
        "host": host, "node": node, "structured_citation": structured,
        "host_named": named, "node_named": node_named,
        "node_in_cited_document": node_in_cited,
        "inventory_evidence": inventory, "document_evidence": document,
        "cited_docs_mentioning_host": doc_hits, "substantive": subst,
        "content_tokens": content, "bound": bound,
    }


# --- the rules -------------------------------------------------------------------------


def grade_row(row, site_nodes, site_guests, doc_mentions, docs_by_host):
    shape, site = row["shape"], row.get("site")
    expect = row.get("expect") or {}
    answer = row.get("swarm_answer") or ""
    question = row.get("question") or ""
    citations = row.get("citations")
    status = row.get("swarm_status")
    out = {"grader": "proxmox_api", "rules_version": RULES_VERSION}

    if shape == "undocumented":
        out.update(grade_undocumented(status, answer, site, site_guests, doc_mentions))
        return out

    id_tokens = identifier_tokens_for(site, site_nodes, site_guests)

    if shape == "accuracy" and expect.get("kind") == "page_still_true":
        out.update(grade_page(status, answer, question, citations, expect, site_nodes,
                              docs_by_host, id_tokens))
        return out

    host, node = expect.get("host") or "", expect.get("node")
    named_nodes = nodes_named(answer, site_nodes.get(site, []))

    # v5: when the kernel said what it read, grade on that and say so. Otherwise fall
    # back to concordance and say THAT. The two are never pooled into one headline.
    prov = provenance_evidence(row.get("provenance"), site, host, node, docs_by_host)
    if prov and status == "found":
        out.update(prov)
        out["expected_node"] = node
        out["class"] = evidence_class(prov["inventory_evidence"], prov["document_evidence"])
        if out["class"] == "answered_off":
            out["why"] = "the kernel record shows neither the subject's placement nor a document about it"
        elif out["class"] == "inventory_only":
            out["why"] = "grounded on the placement fact; no grounded passage reaches this host"
        elif out["class"] == "corpus_only":
            out["why"] = "grounded on a document about this host; no placement fact among the grounded facts"
        return out

    ev = host_evidence(answer, question, citations, site, host, node, docs_by_host, id_tokens)
    ev["evidence_basis"] = "concordance"
    out["evidence"] = ev
    out["nodes_named"] = named_nodes
    out["expected_node"] = node

    if status != "found":
        out["class"] = "no_answer"
        return out

    # Contradiction first.
    wrong_nodes = [n for n in named_nodes if n != node]
    if wrong_nodes and not ev["node_named"]:
        out["class"] = "wrong"
        out["why"] = f"places {host} on {wrong_nodes}, API says {node}"
        return out

    # Provenance for the wrong thing is not provenance.
    other_structured = [r for r in structured_refs(citations)
                        if r.startswith("net:host:") and r != f"net:host:{site}/{host}"]
    others_named = [g["name"] for g in site_guests.get(site, {}).values()
                    if g["name"].lower() != host.lower() and mentions_host(answer, g["name"])]

    if other_structured and not ev["structured_citation"]:
        out["class"] = "wrong_subject"
        out["why"] = f"structured citation for {other_structured}, not {host}"
        return out
    if others_named and not ev["host_named"] and ev["node_named"]:
        out["class"] = "wrong_subject"
        out["why"] = f"names the right node but answers about {others_named}, not {host}"
        return out
    if ev["node_named"] and not ev["bound"]:
        out["class"] = "unbound_subject"
        out["why"] = "names the right node but never identifies the subject"
        return out

    out["class"] = evidence_class(ev["inventory_evidence"], ev["document_evidence"])
    out["why"] = why_for(out["class"], ev)
    return out


def grade_page(status, answer, question, citations, expect, site_nodes, docs_by_host, id_tokens):
    page = expect.get("hosts", [])
    evs = [
        host_evidence(answer, question, citations, h["site"], h["host"], h["node"],
                      docs_by_host, id_tokens, page_row=True)
        for h in page
    ]
    all_nodes = sorted({n for s in site_nodes for n in site_nodes[s]})
    named = nodes_named(answer, all_nodes)
    allowed = {h["node"] for h in page}
    out = {"evidence_per_host": evs, "nodes_named": named}

    if status != "found":
        out["class"] = "no_answer"
        return out

    wrong_nodes = [n for n in named if n not in allowed]
    if wrong_nodes:
        out["class"] = "wrong"
        out["why"] = f"places page hosts on {wrong_nodes}, API says {sorted(allowed)}"
        return out

    both = [e for e in evs if e["inventory_evidence"] and e["document_evidence"]]
    inv = [e for e in evs if e["inventory_evidence"]]
    doc = [e for e in evs if e["document_evidence"]]

    if both:
        out["class"] = "join_correct"
    elif inv and doc:
        out["class"] = "cross_paired"
        out["why"] = (f"inventory evidence about {[e['host'] for e in inv]}, document evidence "
                      f"about {[e['host'] for e in doc]} — no single host has both")
    elif inv:
        out["class"] = "inventory_only"
    elif doc:
        out["class"] = "corpus_only"
    else:
        out["class"] = "answered_off"
        out["why"] = "no provenance of either kind for any host this page names"
    return out


def evidence_class(inventory, document):
    if inventory and document:
        return "join_correct"
    if inventory:
        return "inventory_only"
    if document:
        return "corpus_only"
    return "answered_off"


def why_for(cls, ev):
    if cls == "inventory_only":
        return f"inventory provenance ({ev['inventory_evidence']}); no document reaches this host"
    if cls == "corpus_only":
        if ev["node_in_cited_document"]:
            return "node named, but a cited document contains it — the node may come from prose"
        return "document provenance only; nothing in the answer comes from the inventory"
    if cls == "answered_off":
        if ev["cited_docs_mentioning_host"] and not ev["substantive"]:
            return "citation reaches this host but the answer carries no content of its own"
        return "answered with neither kind of provenance"
    return None


def grade_undocumented(status, answer, site, site_guests, doc_mentions):
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

    # v3: purpose and undocumented are reported apart, never inside the join numerator.
    joins = [r for r in graded if r.get("join") and r["shape"] in ("placement", "accuracy")]
    purpose = [r for r in graded if r["shape"] == "purpose"]
    undoc = [r for r in graded if r["shape"] == "undocumented"]
    singles = [r for r in graded if not r.get("join") and r["shape"] in ("placement", "accuracy")]
    controls = [r for r in graded if r.get("control_class")]

    def n(rows, *classes):
        return sum(1 for r in rows if r["class"] in classes)

    def prov(rows, kind):
        return sum(1 for r in rows
                   if (r.get("evidence") or {}).get("inventory_evidence") == kind)

    def by(rows, field):
        out = {}
        for value in sorted({(r.get(field) or "n/a") for r in rows}):
            sub = [r for r in rows if (r.get(field) or "n/a") == value]
            out[value] = {"n": len(sub), "join_correct": n(sub, "join_correct"),
                          "counts": counts(sub)}
        return out

    ctrl_ok = sum(1 for r in controls if r["control_class"] in ("inventory_only", "join_correct"))
    full = [r for r in controls if r.get("control_names_full_subject")]
    part = [r for r in controls if not r.get("control_names_full_subject")]

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
            "inventory_evidence_structured": prov(joins, "structured"),
            "inventory_evidence_exclusive": prov(joins, "exclusive"),
            "corpus_only": n(joins, "corpus_only"),
            "cross_paired": n(joins, "cross_paired"),
            "unbound_subject": n(joins, "unbound_subject"),
            "wrong_subject": n(joins, "wrong_subject"),
            "single_source_questions": len(singles),
            "single_source_reached_inventory": n(singles, "inventory_only", "join_correct"),
            "control_pairs": len(controls),
            "control_reached_inventory": ctrl_ok,
            "control_rate": round(ctrl_ok / len(controls), 3) if controls else None,
            "control_full_subject_name": {
                "n": len(full),
                "reached_inventory": sum(1 for r in full if r["control_class"]
                                         in ("inventory_only", "join_correct")),
            },
            "control_partial_subject_name": {
                "n": len(part),
                "reached_inventory": sum(1 for r in part if r["control_class"]
                                         in ("inventory_only", "join_correct")),
            },
            "purpose_questions": len(purpose),
            "purpose_counts": counts(purpose),
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
    print(f"  JOIN-CONCORDANCE (evidence from both, same host)  {h['join_correct']}/{h['join_questions']}"
          f"  rate={h['join_rate']}")
    print(f"    inventory_only  {h['inventory_only']}"
          f"  (structured {h['inventory_evidence_structured']}, "
          f"exclusive {h['inventory_evidence_exclusive']})")
    print(f"    corpus_only {h['corpus_only']} · cross_paired {h['cross_paired']}"
          f" · unbound_subject {h['unbound_subject']} · wrong_subject {h['wrong_subject']}")
    print(f"  SINGLE-SOURCE reached inventory   "
          f"{h['single_source_reached_inventory']}/{h['single_source_questions']}")
    print(f"  CONTROL reached inventory         "
          f"{h['control_reached_inventory']}/{h['control_pairs']}  rate={h['control_rate']}")
    print(f"    full subject name  {h['control_full_subject_name']['reached_inventory']}"
          f"/{h['control_full_subject_name']['n']}"
          f"   partial  {h['control_partial_subject_name']['reached_inventory']}"
          f"/{h['control_partial_subject_name']['n']}")
    print(f"  PURPOSE (apart)      n={h['purpose_questions']} {h['purpose_counts']}")
    print(f"  UNDOCUMENTED (apart) {h['undocumented_correct']}/{h['undocumented_questions']}")
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
            ctrl = {**row, "question": row["control_question"],
                    "swarm_answer": row.get("control_answer"),
                    "swarm_status": row.get("control_status"),
                    "citations": row.get("control_citations")}
            cg = grade_row(ctrl, site_nodes, site_guests, doc_mentions, docs_by_host)
            g["control_class"] = cg["class"]
            g["control_evidence"] = cg.get("evidence")
            g["control_names_full_subject"] = mentions(row["control_question"],
                                                       expect.get("host") or "")

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
