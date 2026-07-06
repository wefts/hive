# Purge all Phase-2 origins (iac:%, wiki:tables, wiki:corrob) + now-orphaned net:* nodes, so a
# fresh reload is idempotent (no stale duplicates). Keeps Phase-1 (wiki prose, enrich:%) net edges
# — an edge co-attested by a prose origin survives (loses the purged origin, seen_count recomputed).
# Mirrors the enrichment reconcile delete pattern.
Logger.configure(level: :error)
alias Swarm.Repo

{:ok, _} =
  Repo.transaction(fn ->
    %{rows: rows} =
      Repo.query!(
        "SELECT DISTINCT edge_id FROM edge_provenance WHERE origin LIKE 'iac:%' OR origin LIKE 'wiki:%'"
      )

    ids = Enum.map(rows, fn [id] -> id end)
    Repo.query!("DELETE FROM edge_provenance WHERE origin LIKE 'iac:%' OR origin LIKE 'wiki:%'")

    if ids != [] do
      Repo.query!(
        "DELETE FROM edge e WHERE e.id = ANY($1::bigint[]) AND NOT EXISTS (SELECT 1 FROM edge_provenance ep WHERE ep.edge_id = e.id)",
        [ids]
      )

      Repo.query!(
        "UPDATE edge e SET seen_count = (SELECT count(DISTINCT coalesce(origin, provenance)) FROM edge_provenance ep WHERE ep.edge_id = e.id) WHERE e.id = ANY($1::bigint[])",
        [ids]
      )
    end

    %{num_rows: dropped} =
      Repo.query!(
        "DELETE FROM node n WHERE n.key LIKE 'net:%' AND NOT EXISTS (SELECT 1 FROM edge e WHERE e.src = n.id OR e.dst = n.id)"
      )

    IO.puts("NETMAP-PURGE removed_edges~#{length(ids)} orphan_nodes=#{dropped}")
  end)
