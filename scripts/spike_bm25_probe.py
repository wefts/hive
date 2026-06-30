#!/usr/bin/env python3
# pg_search BM25 lexical-arm probe (ADR-0016 Phase 2 spike) — SANDBOX (pg-spike).
# Mirrors the native lexical-arm metric: BM25 over body+title (title field-boosted),
# scope-filtered IN-index, grouped by node, top-K, vs the 7-question gold.
# Aggregate output only. Usage: BOOST=4 SCOPE=group python3 spike_bm25_probe.py
import json, os, subprocess, sys

QA = "/tmp/claude-1004/-home-sebor-Swarm/725cc3dd-d24d-4930-9a96-c9cd11307575/scratchpad/qa.json"
BOOST = os.environ.get("BOOST", "4")
SCOPE = os.environ.get("SCOPE", "group")
K = int(os.environ.get("RECALL_K", "10"))

def psql(sql):
    r = subprocess.run(
        ["docker", "exec", "-i", "pg-spike", "psql", "-U", "postgres", "-d", "spike",
         "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1"],
        input=sql, capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stderr); sys.exit(1)
    return [ln for ln in r.stdout.splitlines() if ln and not ln.startswith("SET")]

def top_keys(q):
    qq = q.replace("'", " ").replace("?", " ").strip()
    sql = f"""SET paradedb.check_topk_scan = false;
SELECT title FROM (
  SELECT node_id, title, max(paradedb.score(id)) AS s
  FROM chunk
  WHERE id @@@ paradedb.boolean(
    must => ARRAY[paradedb.parse('scope:{SCOPE}')],
    should => ARRAY[
      paradedb.match('text', '{qq}'),
      paradedb.boost(query => paradedb.match('title', '{qq}'), factor => {BOOST})
    ])
  GROUP BY node_id, title
  ORDER BY s DESC LIMIT {K}
) x;"""
    return psql(sql)

queries = json.load(open(QA))
n = recall = rr = leads = 0
print(f"== BM25 lexical-arm probe (sandbox) — boost={BOOST}, scope={SCOPE}, recall@{K} ==")
for item in queries:
    q, gold = item["q"], item.get("gold", [])
    keys = top_keys(q)
    rank = next((i + 1 for i, kk in enumerate(keys) if kk in gold), None)
    n += 1
    if rank:
        recall += 1; rr += 1.0 / rank
        if rank == 1: leads += 1
    print(f"  {q[:42]:42} | rank={rank if rank else '—'} (of {len(keys)})")

print(f"\n  -- AGGREGATE --")
print(f"    recall@{K}: {recall/n:.3f}  ({recall}/{n})")
print(f"    MRR:       {rr/n:.3f}")
print(f"    leads(#1): {leads/n:.3f}  ({leads}/{n})")
