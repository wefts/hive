# Edge-level wiki∩repo corroboration: for each repo `tunnel carries subnet(CIDR)` edge, if the WIKI
# documents it (tunnel name AND that exact CIDR co-occur in one corpus body), re-emit the same edge
# with the co-occurring PAGE as its lineage (`wiki:page:<node>`, S1) → so it corroborates the repo
# (iac + wiki = 2) but does NOT double-count against that same page's other extraction passes.
Logger.configure(level: :error)
alias Swarm.Enrichment.NetworkMap
alias Swarm.Graph.Store
alias Swarm.Repo

%{rows: rows} =
  Repo.query!("""
  SELECT replace(s.key,'net:tunnel:',''), replace(d.key,'net:subnet:','')
    FROM edge e JOIN node s ON s.id=e.src JOIN node d ON d.id=e.dst
   WHERE e.type='carries' AND s.key LIKE 'net:tunnel:%' AND d.key LIKE 'net:subnet:%' AND e.reward >= 0
  """)

# For each, the FIRST corpus page where tunnel name + exact CIDR co-occur → that page is the lineage.
documented =
  rows
  |> Enum.map(fn [tun, cidr] ->
    %{rows: r} =
      Repo.query!(
        "SELECT node_id FROM content WHERE body ILIKE $1 AND body ILIKE $2 ORDER BY node_id LIMIT 1",
        ["%" <> tun <> "%", "%" <> cidr <> "%"]
      )

    case r do
      [[node]] ->
        {"wiki:page:#{node}",
         %{subject: tun, subject_kind: "tunnel", relation: "carries", object: cidr, object_kind: "subnet"}}

      _ ->
        nil
    end
  end)
  |> Enum.reject(&is_nil/1)

node = %{id: Store.upsert_node("source", "wiki:corrob", scope: "group"), scope: "group"}

ids =
  documented
  |> Enum.group_by(fn {lin, _} -> lin end, fn {_, f} -> f end)
  |> Enum.flat_map(fn {lineage, facts} ->
    NetworkMap.write(node, facts, "wiki:corrob:facts",
      origin: "wiki:corrob", lineage: lineage, reliability: 0.5, evidence_kind: "observation")
  end)

IO.puts("NETMAP-CORROB carries_edges=#{length(rows)} wiki_documented=#{length(documented)} edges=#{length(ids)}")
