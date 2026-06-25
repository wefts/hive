#!/usr/bin/env bash
# Cognitive-activation spike — WIPE (the non-negotiable guard close-out).
# Deletes all spike-created state: non-article nodes (cascades claim edges, their
# provenance, entity content/chunks). Pre-slice was article-only with links_to/child_of
# edges, so this restores the exact snapshot. Verifies counts == pre-snapshot.
set -euo pipefail
DB=${SWARM_DB_NAME:-swarm_slice}
P() { docker exec hive-postgres-1 psql -U swarm -d "$DB" -tAc "$1"; }
CNT="select 'node',count(*) from node union all select 'edge',count(*) from edge union all select 'content',count(*) from content union all select 'chunk',count(*) from chunk union all select 'non_article_nodes',count(*) from node where type<>'article' union all select 'claim_edges',count(*) from edge where type not in ('links_to','child_of');"
echo "== pre-wipe =="
P "$CNT"
echo "== wiping spike state =="
P "DELETE FROM edge WHERE type NOT IN ('links_to','child_of');"
P "DELETE FROM node WHERE type<>'article';"   # cascades any remaining entity edges/content/chunk
echo "== post-wipe (must equal snapshot: node 2982 / edge 3163 / content 562 / chunk 4727) =="
P "$CNT"
