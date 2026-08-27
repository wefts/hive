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

# S1: a fact may carry its own upstream `lineage` (wiki loaders emit per-PAGE lineage so different
# pages corroborate but the same page across passes counts once). Facts are grouped by lineage and
# written per group; a fact without `lineage` (e.g. iac) falls back to origin-derived (identity).
# ADR-20: the anchor (and every fact derived from it) lives at the registered Source's
# `src:<uuid>` scope. The Source kind follows the origin prefix (`wiki:…` → wiki, `iac:…` →
# iac); NETMAP_SOURCE_ID picks an explicit instance.
netmap_scope =
  case System.get_env("NETMAP_SOURCE_ID") do
    nil -> Swarm.Projects.scope_by_kind!(origin |> String.split(":") |> hd())
    id -> Swarm.Projects.scope!(id)
  end

node = %{id: Store.upsert_node("source", origin, scope: netmap_scope), scope: netmap_scope}

groups =
  rf
  |> Enum.map(fn f ->
    {f["lineage"],
     %{
       subject: f["subject"], subject_kind: f["subject_kind"],
       relation: f["relation"], object: f["object"], object_kind: f["object_kind"]
     }}
  end)
  |> Enum.group_by(fn {lin, _} -> lin end, fn {_, fact} -> fact end)

{ids, lineages} =
  Enum.reduce(groups, {[], 0}, fn {lineage, facts}, {acc, n} ->
    written =
      NetworkMap.write(node, facts, origin <> ":facts",
        origin: origin,
        lineage: lineage,
        reliability: reliability,
        evidence_kind: evidence_kind
      )

    {acc ++ written, n + 1}
  end)

IO.puts("NETMAP-LOAD origin=#{origin} facts=#{length(rf)} lineages=#{lineages} edges=#{length(ids)} rel=#{reliability}")
