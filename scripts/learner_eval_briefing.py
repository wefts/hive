#!/usr/bin/env python3
"""Build the LEARNER's briefing pack for the join eval.

The learner is a newcomer: it reads documentation, not the inventory. So the
pack holds only corpus material (Confluence/Wiki title + an excerpt). The
Proxmox truth snapshot is NEVER shown to the learner -- it is the grader.

Selection rule, recorded so it can be audited:
  * documents that mention at least one forge/galaxy Proxmox subject whose name
    is >= 6 characters (shorter names collide with ordinary words);
  * ranked by how many distinct subjects they mention, then by title;
  * top --docs of them, excerpt = first --chars characters of the body.

Output: JSON {generated_at, selection, docs:[{ref,title,excerpt}]} on --out.
Contains real intranet content -> hive/tmp only, never committed.
"""
import argparse, csv, json, pathlib, subprocess, sys, datetime

def psql(sql, db):
    p = subprocess.run(
        ["docker", "exec", "-i", "hive-postgres-1", "psql", "-U", "swarm", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-q", "-At", "-F", "\x1f"],
        input=sql, text=True, capture_output=True)
    if p.returncode != 0:
        sys.exit(f"psql failed: {p.stderr[-2000:]}")
    return [l.split("\x1f") for l in p.stdout.splitlines() if l]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs-csv", required=True, help="docs_with_hosts.csv from the overlap step")
    ap.add_argument("--db", default="swarm_staging")
    ap.add_argument("--docs", type=int, default=40)
    ap.add_argument("--skip", type=int, default=0,
                    help="skip this many ranked docs first; the live and frozen packs "
                         "are kept DISJOINT by document so a fix tuned on one is not "
                         "measured on the same subjects")
    ap.add_argument("--chars", type=int, default=1200)
    ap.add_argument("--mode", choices=("bodies", "titles"), default="bodies",
                    help="bodies: title + excerpt (what a reader sees). titles: title + the "
                         "host names the page mentions, no prose -- a far smaller outbound "
                         "footprint, tested against `bodies` for question quality.")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.docs_csv)))
    rows.sort(key=lambda r: (-int(r["hosts"]), r["title"]))
    picked = rows[a.skip : a.skip + a.docs]

    if a.mode == "bodies":
        refs = ",".join("'" + r["source_ref"].replace("'", "''") + "'" for r in picked)
        bodies = {
            ref: body
            for ref, body in psql(
                f"select c.source_ref, replace(left(c.body,{a.chars}), E'\\n', ' ') "
                f"from content c where c.source_ref in ({refs})", a.db)
        }
        docs = [
            {"ref": r["source_ref"], "title": r["title"], "excerpt": bodies.get(r["source_ref"], "")}
            for r in picked
        ]
    else:
        # Bare hostnames with the site alongside: a `site/name` path is not how anyone
        # names a machine, and it leaked into the learner's question text when it was.
        docs = [
            {"ref": r["source_ref"], "title": r["title"], "excerpt": "",
             "hosts_mentioned": [
                 {"site": e.split("/", 1)[0], "name": e.split("/", 1)[1]}
                 for e in r["hostlist"].split() if "/" in e
             ]}
            for r in picked
        ]

    out = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "selection": {
            "rule": "documents mentioning >=1 proxmox subject (name len>=6), ranked by distinct subjects",
            "mode": a.mode,
            "docs": len(docs), "excerpt_chars": a.chars if a.mode == "bodies" else 0, "db": a.db,
            "total_excerpt_chars": sum(len(d["excerpt"]) for d in docs),
        },
        "docs": docs,
    }
    pathlib.Path(a.out).write_text(json.dumps(out, indent=2) + "\n")
    print(f"learner_eval_briefing: {len(docs)} docs, "
          f"{out['selection']['total_excerpt_chars']} excerpt chars -> {a.out}")

if __name__ == "__main__":
    main()
