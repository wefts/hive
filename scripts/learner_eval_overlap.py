#!/usr/bin/env python3
"""Stage 0 of the learner eval: where do the inventory and the corpus talk about
the same machine at all?

Two model-free tables, both derived from a Proxmox truth snapshot and the
ingested document bodies. Everything downstream depends on them:

  overlap.csv         one row per site-qualified Proxmox subject, with how many
                      Confluence and MediaWiki documents mention it. This is the
                      ceiling on any join, and the ground truth for the
                      `undocumented` question shape.
  docs_with_hosts.csv one row per document that mentions at least one subject,
                      with the subjects it names. Selects the learner's briefing
                      pack, resolves `accuracy` questions that are about a page
                      rather than a host, and lets the grader check that a
                      citation really reaches a document mentioning the host.

Matching is word-boundary, case-insensitive, with `-` and `_` treated as part of
a name so `app-pp` does not match inside `app-pp-old`. Names shorter than
--min-name characters are still counted in overlap.csv (with their length, so
they can be filtered) but excluded from document selection, where a subject
called `tools` or `mail` would drag in most of the corpus.

Output carries real host names -> hive/tmp only, never committed.

Usage:
  scripts/learner_eval_overlap.py --truth tmp/learner-eval/truth.json \
      --out-overlap tmp/learner-eval/overlap.csv \
      --out-docs tmp/learner-eval/docs_with_hosts.csv
"""
import argparse
import json
import pathlib
import re
import subprocess
import sys


def subjects(truth_path):
    """Every site-qualified subject the API reports: hypervisor nodes and live guests."""
    doc = json.loads(pathlib.Path(truth_path).read_text())
    out, seen = [], set()
    for site, s in doc["sites"].items():
        for n in s["nodes"]:
            if (site, n["node"]) not in seen:
                seen.add((site, n["node"]))
                out.append((site, "node", n["node"]))
        for g in s["guests"]:
            # Templates are not machines; a blank name cannot be matched on.
            if g["name"] and not g["template"] and (site, g["name"]) not in seen:
                seen.add((site, g["name"]))
                out.append((site, "guest", g["name"]))
    return doc, out


def pattern(name):
    return "(^|[^A-Za-z0-9_-])" + re.escape(name) + "([^A-Za-z0-9_-]|$)"


def sql_values(rows):
    def esc(s):
        return s.replace("'", "''")

    return ",".join(
        "('%s','%s','%s','%s')" % (esc(site), esc(kind), esc(name), esc(pattern(name)))
        for site, kind, name in rows
    )


def psql(sql, db):
    p = subprocess.run(
        ["docker", "exec", "-i", pathlib.os.environ.get("SWARM_PG_CONTAINER", "hive-postgres-1"),
         "psql", "-U", "swarm", "-d", db, "-v", "ON_ERROR_STOP=1", "-q"],
        input=sql, text=True, capture_output=True)
    if p.returncode != 0:
        sys.exit(f"learner_eval_overlap: psql failed: {p.stderr[-2000:]}")
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", required=True)
    ap.add_argument("--db", default="swarm_staging")
    ap.add_argument("--min-name", type=int, default=6,
                    help="names shorter than this are excluded from document selection")
    ap.add_argument("--out-overlap", required=True)
    ap.add_argument("--out-docs", required=True)
    a = ap.parse_args()

    truth, rows = subjects(a.truth)
    if not rows:
        sys.exit(f"learner_eval_overlap: {a.truth} names no subjects")
    long_rows = [r for r in rows if len(r[2]) >= a.min_name]

    corpus = "(c.source_ref like 'confluence%' or c.source_ref like 'mediawiki%')"

    overlap = psql(f"""
create temp table hostnames(site text, kind text, name text, pat text);
insert into hostnames values {sql_values(rows)};
copy (
  select h.site, h.kind, h.name, length(h.name) as len,
         count(*) filter (where c.source_ref like 'confluence%') as conf_docs,
         count(*) filter (where c.source_ref like 'mediawiki%') as wiki_docs
  from hostnames h
  left join content c
    on c.source_ref is not null and c.source_ref <> '' and c.body ~* h.pat
  group by 1,2,3,4 order by 1,2,3
) to stdout with csv header;
""", a.db)

    docs = psql(f"""
create temp table hostnames(site text, kind text, name text, pat text);
insert into hostnames values {sql_values(long_rows)};
copy (
  select c.source_ref, n.key as title, length(c.body) as blen,
         count(distinct h.name) as hosts,
         string_agg(distinct h.site||'/'||h.name, ' ' order by h.site||'/'||h.name) as hostlist
  from content c
  join node n on n.id = c.node_id
  join hostnames h on c.body ~* h.pat
  where {corpus}
  group by 1,2,3 order by hosts desc, title
) to stdout with csv header;
""", a.db)

    pathlib.Path(a.out_overlap).write_text(overlap)
    pathlib.Path(a.out_docs).write_text(docs)

    mentioned = sum(
        1 for line in overlap.splitlines()[1:]
        if line and sum(int(x) for x in line.rsplit(",", 2)[-2:]) > 0
    )
    print(f"learner_eval_overlap: observed_at={truth['observed_at']} "
          f"subjects={len(rows)} mentioned_in_a_document={mentioned} "
          f"({100.0 * mentioned / len(rows):.1f}%) -> {a.out_overlap}")
    print(f"learner_eval_overlap: documents naming >=1 subject "
          f"(name>={a.min_name} chars) = {max(0, len(docs.splitlines()) - 1)} -> {a.out_docs}")


if __name__ == "__main__":
    main()
