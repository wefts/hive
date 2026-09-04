#!/usr/bin/env python3
"""What licenses a document->host link, and — mostly — what does not.

The join the whole campaign is measuring needs one thing the graph has not got: a way
to get from a service as the documentation names it ("Keycloak") to the machine the
inventory knows ("auth.<domain>"). A linker that guesses manufactures exactly the false
joins the apparatus was built to detect, and does it confidently: the failure mode is
not a missing link, it is a wrong one that looks right.

So this refuses by default. Two bases license a link and nothing else does:

  alias           the document-derived graph key and the site-qualified Proxmox key are
                  the SAME NAME (ADR-17). Not an inference at all -- a reconciliation.
  title_equality  the document's whole title, normalised, EQUALS the host's stem, and
                  resolves to exactly one host. "Vault" -> vault.<domain>.

Everything else is refused with a recorded reason. In particular:

  * **Exclusive mention is not evidence.** 149 of 232 documents mention exactly one
    inventory host, and only 28 of those have a title related to it. "Argo CD" mentions
    `gitlab`; an employee-onboarding page mentions the password vault. Linking on
    exclusive mention would assert "Argo CD runs on gitlab" -- plausible, confident, and
    wrong.
  * **Title CONTAINMENT is not evidence.** "Monitoring alerts in mattermost" contains
    "monitor", which is a host stem. That is a substring accident.
  * **A title resolving to hosts at two sites is refused**, not resolved by preference:
    "Gitlab" matches a host at each of two sites, and picking one invents a fact.

The refusals are the output too. If most subjects cannot be linked, that is a finding
about the corpus, not a gap to paper over.

Usage:
  scripts/learner_eval_link_evidence.py --docs-with-hosts ... --truth ... \
      [--out-links links.csv] [--out-refusals refusals.csv]
"""
import argparse
import collections
import csv
import json
import pathlib
import re
import sys

ALIAS = "alias"
TITLE_EQUALITY = "title_equality"


def normalise(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def stem(host):
    return (host or "").split(".")[0]


def load_docs(path):
    out = []
    for r in csv.DictReader(open(path)):
        hosts = []
        for entry in r["hostlist"].split():
            site, _, name = entry.partition("/")
            if name:
                hosts.append((site, name))
        out.append({"source_ref": r["source_ref"], "title": r["title"], "hosts": hosts})
    return out


def load_truth_names(path):
    doc = json.loads(pathlib.Path(path).read_text())
    names = set()
    for site, s in doc["sites"].items():
        for g in s["guests"]:
            if g["name"] and not g["template"]:
                names.add((site, g["name"]))
    return names


def decide(docs, truth_names):
    """Every document/host pair, with the basis that licenses a link or the reason it is refused."""
    # A normalised title may name several hosts. Group first: a title that resolves to
    # more than one host is ambiguous and must refuse, never be resolved by preference.
    title_targets = collections.defaultdict(set)
    for d in docs:
        for site, host in d["hosts"]:
            if normalise(d["title"]) == normalise(stem(host)):
                title_targets[normalise(d["title"])].add((site, host))

    links, refusals = [], []
    for d in docs:
        title_norm = normalise(d["title"])
        for site, host in d["hosts"]:
            row = {"source_ref": d["source_ref"], "title": d["title"], "site": site, "host": host}

            if (site, host) not in truth_names:
                refusals.append({**row, "reason": "host is not a live guest in the snapshot"})
                continue

            if title_norm and title_norm == normalise(stem(host)):
                targets = title_targets[title_norm]
                if len(targets) > 1:
                    refusals.append({
                        **row,
                        "reason": f"title resolves to {len(targets)} hosts across sites: "
                                  f"{sorted(f'{s}/{h}' for s, h in targets)}",
                    })
                else:
                    links.append({**row, "basis": TITLE_EQUALITY})
                continue

            if len(d["hosts"]) == 1:
                refusals.append({
                    **row,
                    "reason": "sole host on the page, but the title is unrelated — a passing "
                              "mention, not a statement that the page is about this host",
                })
            elif title_norm and normalise(stem(host)) and normalise(stem(host)) in title_norm:
                refusals.append({
                    **row,
                    "reason": "title merely CONTAINS the host stem (substring accident)",
                })
            else:
                refusals.append({
                    **row,
                    "reason": f"page names {len(d['hosts'])} hosts and the title matches none",
                })
    return links, refusals


def self_test():
    """Run the adversarial fixtures. Non-zero exit if any case decides differently."""
    fix = pathlib.Path(__file__).resolve().parent / "fixtures" / "learner_eval" / "link"
    spec = json.loads((fix / "expected.json").read_text())["expected"]
    links, refusals = decide(load_docs(fix / "docs_with_hosts.csv"),
                             load_truth_names(fix / "truth.json"))
    got = {f"{l['source_ref']}|{l['host']}": ("link", l["basis"]) for l in links}
    got.update({f"{r['source_ref']}|{r['host']}": ("refuse", r["reason"]) for r in refusals})

    failed = []
    print(f"link self-test: {len(spec)} adversarial cases")
    for case, want in spec.items():
        decision, detail = got.get(case, ("missing", ""))
        ok = decision == want["decision"] and (
            "basis" not in want or detail == want["basis"])
        if not ok:
            failed.append(case)
        print(f"  {'ok  ' if ok else 'FAIL'} {case}")
        print(f"       want {want['decision']}"
              f"{'/' + want['basis'] if 'basis' in want else ''}, got {decision} — {detail[:70]}")
        if not ok:
            print(f"       probes: {want['probes']}")
    if failed:
        print(f"\nlink self-test: FAILED on {len(failed)}: {', '.join(failed)}")
        return 1
    print("\nlink self-test: every case decided as pre-registered")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true",
                    help="run the adversarial fixtures instead of a corpus")
    ap.add_argument("--docs-with-hosts")
    ap.add_argument("--truth")
    ap.add_argument("--out-links")
    ap.add_argument("--out-refusals")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not (a.docs_with_hosts and a.truth):
        ap.error("--docs-with-hosts and --truth are required unless --self-test")

    docs = load_docs(a.docs_with_hosts)
    truth_names = load_truth_names(a.truth)
    links, refusals = decide(docs, truth_names)

    pairs = len(links) + len(refusals)
    hosts_linked = {(l["site"], l["host"]) for l in links}
    print(f"link evidence: {len(docs)} documents, {pairs} document/host pairs")
    print(f"  LINKED  {len(links):4} pairs -> {len(hosts_linked)} distinct hosts "
          f"(basis: {dict(collections.Counter(l['basis'] for l in links))})")
    print(f"  REFUSED {len(refusals):4} pairs")
    for reason, n in collections.Counter(
        re.sub(r"\d+", "N", r["reason"]).split("—")[0].split(":")[0].strip()
        for r in refusals
    ).most_common():
        print(f"      {n:4}  {reason}")

    if not a.quiet and links:
        print("\n  the links, in full (small enough to read, which is the point):")
        for l in sorted(links, key=lambda x: x["host"]):
            print(f"      {l['title'][:34]:34} -> {l['site']}/{l['host']}")

    if a.out_links:
        with open(a.out_links, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["site", "host", "source_ref", "title", "basis"])
            w.writeheader()
            for l in sorted(links, key=lambda x: (x["site"], x["host"])):
                w.writerow({k: l[k] for k in w.fieldnames})
        print(f"\nlink evidence: wrote {a.out_links}")
    if a.out_refusals:
        with open(a.out_refusals, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["site", "host", "source_ref", "title", "reason"])
            w.writeheader()
            for r in sorted(refusals, key=lambda x: (x["site"], x["host"])):
                w.writerow({k: r[k] for k in w.fieldnames})
        print(f"link evidence: wrote {a.out_refusals}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
