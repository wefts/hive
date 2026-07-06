# Edge-level wiki∩repo corroboration: for each repo `tunnel carries subnet(CIDR)` edge in the graph,
# if the WIKI documents it (tunnel name AND that exact CIDR co-occur in one corpus body), re-emit
# the same edge with a `wiki:corrob` origin → seen_count↑ = corroborated (ADR-13). Deterministic
# co-occurrence on exact shared identifiers (CIDRs) — no name-canonicalization needed.
Logger.configure(level: :error)
alias Swarm.Enrichment.NetworkMap
alias Swarm.Graph.Store
alias Swarm.Repo

# existing repo carries edges: net:tunnel:<t> -carries-> net:subnet:<cidr>
%{rows: rows} =
  Repo.query!("""
  SELECT replace(s.key,'net:tunnel:',''), replace(d.key,'net:subnet:','')
    FROM edge e JOIN node s ON s.id=e.src JOIN node d ON d.id=e.dst
   WHERE e.type='carries' AND s.key LIKE 'net:tunnel:%' AND d.key LIKE 'net:subnet:%' AND e.reward >= 0
  """)

documented =
  rows
  |> Enum.filter(fn [tun, cidr] ->
    match?(%{rows: [[c]]} when c > 0,
      Repo.query!("SELECT count(*) FROM content WHERE body ILIKE $1 AND body ILIKE $2",
        ["%" <> tun <> "%", "%" <> cidr <> "%"]))
  end)
  |> Enum.map(fn [tun, cidr] ->
    %{subject: tun, subject_kind: "tunnel", relation: "carries", object: cidr, object_kind: "subnet"}
  end)

src = Store.upsert_node("source", "wiki:corrob", scope: "group")
ids = NetworkMap.write(%{id: src, scope: "group"}, documented, "wiki:corrob:facts",
  origin: "wiki:corrob", reliability: 0.5, evidence_kind: "observation")

IO.puts("NETMAP-CORROB carries_edges=#{length(rows)} wiki_documented=#{length(documented)} edges=#{length(ids)}")
