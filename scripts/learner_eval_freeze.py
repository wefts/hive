#!/usr/bin/env python3
"""Resolve the learner's candidate questions into a gradeable set, and freeze it.

The orchestrator's job here is SELECTION, never authorship: every question text
comes from the learner untouched. What this step adds is the expectation the
Proxmox API will grade it against, and a recorded reason for every candidate it
drops. It refuses to look at any Swarm answer -- the set is locked before the
system under test has said anything.

Drop rules, all mechanical:
  * site not in --sites;
  * `subject` does not resolve to a guest or node in the truth snapshot
    (exact name, FQDN stem, or vmid);
  * the shape has no machine-checkable expectation.

Each surviving row records `name_identity`:
  exact    -- the learner's wording already contains the raw hostname, so the
              question may be answerable from the inventory alone;
  partial  -- wording shares a stem with the hostname;
  distinct -- wording and hostname share nothing, so an answer requires the
              document that connects them. This is the strongest join evidence.

Usage:
  scripts/learner_eval_freeze.py --candidates ... --truth ... --overlap ... --out ...
"""
import argparse
import csv
import datetime
import hashlib
import json
import pathlib
import re
import sys

GRADEABLE = {"placement", "purpose", "accuracy", "undocumented"}


def load_truth(path):
    doc = json.loads(pathlib.Path(path).read_text())
    index = {}
    for site, s in doc["sites"].items():
        for n in s["nodes"]:
            index[(site, n["node"].lower())] = {
                "site": site, "kind": "node", "name": n["node"],
                "node": n["node"], "status": n["status"],
            }
        for g in s["guests"]:
            if not g["name"] or g["template"]:
                continue
            rec = {
                "site": site, "kind": "guest", "name": g["name"], "node": g["node"],
                "status": g["status"], "vmid": g["vmid"],
            }
            index[(site, g["name"].lower())] = rec
            stem = g["name"].split(".")[0].lower()
            index.setdefault((site, stem), rec)
    return doc, index


def resolve(index, sites, site, subject):
    if not subject:
        return None
    subject = str(subject).strip().lower()
    # The briefing once rendered hosts as `site/name`; accept that shape rather than
    # dropping otherwise-good questions over a formatting artefact.
    if "/" in subject:
        head, _, tail = subject.partition("/")
        if head in sites:
            site, subject = head, tail
    candidates = [site] if site in sites else list(sites)
    for s in candidates:
        for key in (subject, subject.split(".")[0]):
            hit = index.get((s, key))
            if hit:
                return hit
    return None


def name_identity(written, hostname):
    """How much of the hostname the learner's own wording already gives away.

    `exact` needs the name as a WHOLE TOKEN: a product called "Storekeeper" is not the
    host `store` -- it is a product name that only the documentation ties to that host,
    and treating it as exact would quietly credit a join as a lookup.
    """
    w = (written or "").lower()
    h = hostname.lower()
    stem = h.split(".")[0]

    def token(needle):
        return re.search(r"(^|[^a-z0-9])" + re.escape(needle) + r"([^a-z0-9]|$)", w) is not None

    if token(h) or token(stem):
        return "exact"
    parts = [p for p in re.split(r"[^a-z0-9]+", stem) if len(p) >= 4]
    if stem in w or any(p in w for p in parts):
        return "partial"
    return "distinct"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True, nargs="+",
                    help="one or more candidate files, merged in the order given; a later "
                         "duplicate of the same (shape, subject) is dropped, so put the "
                         "preferred learner pack first")
    ap.add_argument("--truth", required=True)
    ap.add_argument("--overlap", required=True, help="overlap.csv: host -> doc mention counts")
    ap.add_argument("--docs-with-hosts", required=True,
                    help="docs_with_hosts.csv: lets an `accuracy` question about a PAGE "
                         "resolve to the hosts that page names")
    ap.add_argument("--sites", default="forge,galaxy")
    ap.add_argument("--label", required=True, help="frozen | live -- recorded on every row")
    ap.add_argument("--out", required=True)
    ap.add_argument("--drops", required=True)
    a = ap.parse_args()

    sites = [s.strip() for s in a.sites.split(",") if s.strip()]
    truth, index = load_truth(a.truth)

    docs_for_host = {}
    for r in csv.DictReader(open(a.overlap)):
        docs_for_host[(r["site"], r["name"].lower())] = int(r["conf_docs"]) + int(r["wiki_docs"])

    hosts_on_doc = {}
    for r in csv.DictReader(open(a.docs_with_hosts)):
        hosts_on_doc[r["source_ref"]] = [
            tuple(e.split("/", 1)) for e in r["hostlist"].split() if "/" in e
        ]

    headers, cands = [], []
    for path in a.candidates:
        lines = [json.loads(l) for l in open(path) if l.strip()]
        for line in lines:
            if line.get("kind") == "learner_run":
                headers.append({**line, "candidates_file": path})
            else:
                cands.append({**line, "candidates_file": path})
    header = headers[0] if len(headers) == 1 else {
        "kind": "learner_run", "packs": headers,
        "gemini_calls": sum(h.get("gemini_calls", 0) for h in headers),
        "prompt_chars_sent": sum(h.get("prompt_chars_sent", 0) for h in headers),
    }

    kept, drops = [], []
    for i, c in enumerate(cands):
        shape = (c.get("shape") or "").strip()
        site = (c.get("site") or "").strip().lower()
        question = (c.get("question") or "").strip()

        def drop(reason):
            drops.append({"index": i, "shape": shape, "question": question, "reason": reason,
                          "subject": c.get("subject"), "site": site})

        if shape not in GRADEABLE:
            drop(f"unknown shape {shape!r}")
            continue
        if not question:
            drop("empty question")
            continue
        if site not in sites and shape != "undocumented":
            drop(f"site {site!r} outside {sites}")
            continue

        if shape == "undocumented":
            if site not in sites:
                drop(f"site {site!r} outside {sites}")
                continue
            undoc = sorted(
                rec["name"]
                for (s, key), rec in index.items()
                if s == site and rec["kind"] == "guest" and key == rec["name"].lower()
                and docs_for_host.get((s, rec["name"].lower()), 0) == 0
            )
            kept.append({
                "shape": shape, "question": question, "site": site,
                "expect": {"kind": "undocumented_set", "site": site,
                           "undocumented_count": len(undoc), "undocumented": undoc},
                "grading": "precision: every host the answer names must have zero corpus mentions",
                "join": True, "name_identity": None,
                "subject": None, "subject_as_written": c.get("subject_as_written"),
                "doc_ref": c.get("doc_ref"), "learner": c.get("learner"), "batch": c.get("batch"),
            })
            continue

        hit = resolve(index, sites, site, c.get("subject"))

        # "Is this page still accurate?" is about a PAGE, not a host, so it is allowed to
        # resolve through the document to the machines that page names.
        if not hit and shape == "accuracy":
            page_hosts = [
                index[(s, n.lower())]
                for s, n in hosts_on_doc.get(c.get("doc_ref") or "", [])
                if (s, n.lower()) in index
            ]
            if page_hosts:
                kept.append({
                    "shape": shape, "question": question, "site": site or page_hosts[0]["site"],
                    "expect": {
                        "kind": "page_still_true", "doc_ref": c.get("doc_ref"),
                        "hosts": [
                            {"site": h["site"], "host": h["name"], "node": h["node"],
                             "status": h["status"]}
                            for h in page_hosts
                        ],
                    },
                    "grading": ("every placement or status the answer states about a host this "
                                "page names must match the API"),
                    "join": True, "name_identity": "distinct",
                    "subject": None, "subject_as_written": c.get("subject_as_written"),
                    "doc_ref": c.get("doc_ref"), "learner": c.get("learner"), "batch": c.get("batch"),
                })
                continue

        if not hit:
            drop(f"subject {c.get('subject')!r} not in the Proxmox snapshot for {site or sites}")
            continue

        ident = name_identity(c.get("subject_as_written") or question, hit["name"])

        if shape == "placement":
            row = {
                "shape": shape, "question": question, "site": hit["site"],
                "expect": {"kind": "node", "node": hit["node"], "host": hit["name"],
                           "site": hit["site"], "status": hit["status"]},
                "grading": "the answer must name the Proxmox node the API places this host on",
                "join": ident != "exact", "name_identity": ident,
                "subject": hit["name"], "subject_as_written": c.get("subject_as_written"),
                "doc_ref": c.get("doc_ref"), "learner": c.get("learner"), "batch": c.get("batch"),
            }
            if c.get("control_question"):
                row["control_question"] = c["control_question"].strip()
                row["control_expect"] = row["expect"]
            kept.append(row)

        elif shape == "purpose":
            row = {
                "shape": shape, "question": question, "site": hit["site"],
                "expect": {"kind": "purpose", "host": hit["name"], "site": hit["site"],
                           "node": hit["node"], "status": hit["status"],
                           "corpus_mentions": docs_for_host.get((hit["site"], hit["name"].lower()), 0)},
                "grading": ("answer must cite a corpus document that really mentions this host, "
                            "and any placement it states must match the API"),
                "join": True, "name_identity": ident,
                "subject": hit["name"], "subject_as_written": c.get("subject_as_written"),
                "doc_ref": c.get("doc_ref"), "learner": c.get("learner"), "batch": c.get("batch"),
            }
            kept.append(row)

        elif shape == "accuracy":
            members = sorted(
                rec["name"] for (s, key), rec in index.items()
                if s == hit["site"] and rec["kind"] == "guest" and key == rec["name"].lower()
                and rec["node"] == hit["node"]
            ) if hit["kind"] == "node" else []
            kept.append({
                "shape": shape, "question": question, "site": hit["site"],
                "expect": {"kind": "still_true", "host": hit["name"], "site": hit["site"],
                           "node": hit["node"], "status": hit["status"], "co_located": members},
                "grading": ("answer must not contradict the API on this host's node or status; "
                            "an answer that claims currency without citing an observation instant "
                            "is recorded as unsupported"),
                "join": True, "name_identity": ident,
                "subject": hit["name"], "subject_as_written": c.get("subject_as_written"),
                "doc_ref": c.get("doc_ref"), "learner": c.get("learner"), "batch": c.get("batch"),
            })

    # Merging two learner packs over the same documents produces the same subject twice.
    # Keeping both would silently weight that subject double in the rate.
    #
    # `undocumented` is one question per SITE however many pages prompt it, and a version
    # that names its site in the text is the answerable one -- so those sort first and the
    # rest are dropped as duplicates. Selection, not authorship: the surviving text is
    # still the learner's, and every drop is recorded in the drops file.
    def dedupe_key(row):
        if row.get("subject"):
            return (row["shape"], row["subject"].lower())
        if row["shape"] == "undocumented":
            return (row["shape"], row.get("site"))
        return (row["shape"], row.get("site"), row.get("doc_ref"))

    def names_its_site(row):
        return (row.get("site") or "") in (row.get("question") or "").lower()

    # Stable: only the undocumented rows move, and only so a site-naming phrasing wins
    # its key over one that leaves the site out of the question entirely.
    kept.sort(key=lambda r: 0 if r["shape"] != "undocumented" else (0 if names_its_site(r) else 1))

    deduped, seen_keys = [], set()
    for row in kept:
        key = dedupe_key(row)
        if key in seen_keys:
            drops.append({"index": None, "shape": row["shape"], "question": row["question"],
                          "reason": f"duplicate of an earlier {key}", "subject": row.get("subject"),
                          "site": row.get("site")})
            continue
        seen_keys.add(key)
        deduped.append(row)
    kept = deduped

    payload = json.dumps(kept, sort_keys=True, ensure_ascii=False).encode()
    set_hash = hashlib.sha256(payload).hexdigest()
    meta = {
        "kind": "learner_eval_set",
        "label": a.label,
        "frozen_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "set_hash": set_hash,
        "learner_run": header,
        "truth_snapshot": a.truth,
        "truth_observed_at": truth["observed_at"],
        "sites": sites,
        "candidates": len(cands), "kept": len(kept), "dropped": len(drops),
    }
    with open(a.out, "w") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for k, row in enumerate(kept):
            row["row_id"] = f"{a.label}-{k + 1:03d}"
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(a.drops, "w") as f:
        for d in drops:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    by_shape = {}
    for row in kept:
        by_shape[row["shape"]] = by_shape.get(row["shape"], 0) + 1
    joins = sum(1 for r in kept if r["join"])
    print(f"learner_eval_freeze: {len(cands)} candidates -> {len(kept)} kept "
          f"({joins} join, {len(kept) - joins} single-source), {len(drops)} dropped")
    print(f"learner_eval_freeze: by shape {by_shape}")
    print(f"learner_eval_freeze: set_hash={set_hash}")
    print(f"learner_eval_freeze: wrote {a.out} and {a.drops}")


if __name__ == "__main__":
    main()
