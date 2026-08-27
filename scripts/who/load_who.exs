# Load distilled who-is-who facts into the world-map substrate (E1). Reads /tmp/who_facts.json
# ({origin, reliability, evidence_kind, lineage, profiles:[…], facts:[…]}) — produced host-side by
# ldap_who.py — and writes via WhoMap.
#
# RECONCILIATION (the decorrelated-review BLOCKER fix): `who:` is a SINGLE authoritative origin
# (`ldap:directory`), so each load is an ATOMIC FULL REPLACE — blanket-delete the who substrate
# (CASCADE drops its edges + profile content), then rebuild from the current snapshot. A departed or
# transferred person is simply absent from the new snapshot, so their edges vanish within one refresh
# — a monotonic claim-graph can't refute them otherwise, and min_corroboration=1 + rel 0.9 would
# else serve a leaver confidently for weeks. The transaction makes the replace invisible to readers
# (no empty window). The `concept:who:kind:*` type markers are shared/stable — kept.
#
# Run: docker exec hive-kernel-1 /app/bin/swarm rpc "$(cat load_who.exs)"
Logger.configure(level: :error)
alias Swarm.Enrichment.WhoMap
alias Swarm.Graph.Store
alias Swarm.Repo

{:ok, raw} = File.read("/tmp/who_facts.json")
decoded = Jason.decode!(raw)
origin = Map.get(decoded, "origin", "ldap:directory")
lineage = Map.get(decoded, "lineage", origin)
reliability = Map.get(decoded, "reliability", 0.9)
evidence_kind = Map.get(decoded, "evidence_kind", "observation")
profiles = Map.get(decoded, "profiles", [])
groups = Map.get(decoded, "groups", [])

facts =
  decoded
  |> Map.get("facts", [])
  |> Enum.map(fn f ->
    %{
      subject: f["subject"], subject_kind: f["subject_kind"],
      relation: f["relation"], object: f["object"], object_kind: f["object_kind"]
    }
  end)

{:ok, {people, edges, services}} =
  Repo.transaction(
    fn ->
    # full replace: drop the entire who: entity substrate (CASCADE removes edges + content).
    Repo.query!(
      "DELETE FROM node WHERE key LIKE 'who:person:%' OR key LIKE 'who:team:%' " <>
        "OR key LIKE 'who:role:%' OR key LIKE 'who:site:%' OR key LIKE 'who:org:%' " <>
        "OR key LIKE 'who:status:%' OR key LIKE 'who:family:%' OR key LIKE 'who:group:%' " <>
        "OR key LIKE 'who:service:%'"
    )

    # ADR-20: every who node/edge lives at the registered directory Source's scope
    # (`src:<uuid>`), never a label. WHO_SOURCE_ID picks the instance; default = the
    # single `ldap` Source.
    who_scope =
      case System.get_env("WHO_SOURCE_ID") do
        nil -> Swarm.Projects.scope_by_kind!("ldap")
        id -> Swarm.Projects.scope!(id)
      end

    anchor = %{id: Store.upsert_node("source", origin, scope: who_scope), scope: who_scope}

    edge_ids =
      WhoMap.write(anchor, facts, origin <> ":facts",
        origin: origin,
        lineage: lineage,
        reliability: reliability,
        evidence_kind: evidence_kind
      )

    written =
      Enum.reduce(profiles, 0, fn prof, acc ->
        case WhoMap.write_profile(prof, origin <> ":profile", scope: who_scope) do
          :error -> acc
          _id -> acc + 1
        end
      end)

      Enum.each(groups, fn g ->
        WhoMap.write_group(g["slug"], g["name"] || g["slug"], g["aliases"] || [], who_scope)
      end)

      services = Map.get(decoded, "services", [])
      teams = Map.get(decoded, "teams", [])
      Enum.each(teams, fn tm -> WhoMap.write_group(tm["slug"], tm["name"] || tm["slug"], [], who_scope) end)
      Enum.each(services, fn s -> WhoMap.write_service(s["slug"], s["name"] || s["slug"], s["aliases"] || [], who_scope) end)

      {written, length(edge_ids), services}
    end,
    # a full directory (~1600 people / ~6k edges) is a batch write — well past the 15s default;
    # keep the whole replace ATOMIC (no empty window) under a generous timeout.
    timeout: 600_000
  )

IO.puts(
  "WHO-LOAD origin=#{origin} profiles=#{people} facts=#{length(facts)} edges=#{edges} " <>
    "groups=#{length(groups)} services=#{length(services)} rel=#{reliability}"
)
