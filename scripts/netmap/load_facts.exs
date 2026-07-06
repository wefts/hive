# Load distilled network facts into the world-map substrate. Reads /tmp/netmap_facts.json
# ({origin, reliability, evidence_kind, facts:[{subject,subject_kind,relation,object,object_kind}]})
# — produced by the host-side parsers — and writes via NetworkMap.write with a `source` provenance
# anchor node for the origin (a HANDLE, not repo content). Distinct origin → corroborates matching
# wiki edges (ADR-13). Run: docker exec hive-kernel-1 /app/bin/swarm rpc "$(cat load_facts.exs)"
Logger.configure(level: :error)
alias Swarm.Enrichment.NetworkMap
alias Swarm.Graph.Store

{:ok, raw} = File.read("/tmp/netmap_facts.json")
%{"origin" => origin, "facts" => rf} = decoded = Jason.decode!(raw)
reliability = Map.get(decoded, "reliability", 0.85)
evidence_kind = Map.get(decoded, "evidence_kind", "observation")

facts =
  Enum.map(rf, fn f ->
    %{
      subject: f["subject"], subject_kind: f["subject_kind"],
      relation: f["relation"], object: f["object"], object_kind: f["object_kind"]
    }
  end)

src = Store.upsert_node("source", origin, scope: "group")

ids =
  NetworkMap.write(%{id: src, scope: "group"}, facts, origin <> ":facts",
    origin: origin, reliability: reliability, evidence_kind: evidence_kind)

IO.puts("NETMAP-LOAD origin=#{origin} facts=#{length(facts)} edges=#{length(ids)} rel=#{reliability}")
