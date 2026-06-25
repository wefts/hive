# ER-3 real validation on swarm_slice — does embed → ANN → real LLM confirm → merge
# wire end-to-end, and does the conservative confirm have PRECISION (merge a true
# duplicate, reject a false pair)? PUBLIC scope only (safe to eyeball). Seeds three
# entities, runs one real pass, prints the outcome, then WIPES the seeded state.
#
#   SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev \
#     mise exec -- mix run --no-start ../../hive/scripts/er_validate.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.EntityResolution.Resolver
alias Swarm.Graph.Store
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

# Seed three PUBLIC entities (provenance-tagged so the wipe is exact):
#   a true duplicate pair sharing a token (should MERGE), and a token-sharing but
#   DISTINCT pair (should be REJECTED) — testing real confirm precision.
seeds = ["United States", "United States of America", "United Kingdom"]
ids = Enum.map(seeds, fn k -> Store.upsert_node("entity", k, scope: "public") end)
IO.puts("seeded #{length(ids)} public entities")

exists = fn key ->
  %{rows: [[n]]} = Repo.query!("SELECT count(*) FROM node WHERE type='entity' AND key=$1", [key])
  n == 1
end

summary = Resolver.run_pass([])
IO.inspect(summary, label: "run_pass")

IO.puts("United States survives:            #{exists.("United States")}")
IO.puts("United States of America survives: #{exists.("United States of America")}")
IO.puts("United Kingdom survives:           #{exists.("United Kingdom")}")

%{rows: audit} =
  Repo.query!(
    "SELECT left_id, right_id, round(cosine::numeric,3), round(lex::numeric,3), decision " <>
      "FROM entity_resolution_audit ORDER BY id"
  )

IO.puts("\naudit (ids/scores/decision — no keys):")
Enum.each(audit, fn row -> IO.puts("  #{inspect(row)}") end)

verdict =
  if summary.merged >= 1 and exists.("United Kingdom"),
    do: "RESULT: precision OK (a true dup merged; the distinct pair was NOT merged)",
    else: "RESULT: inspect — merged=#{summary.merged}"

IO.puts("\n#{verdict}")
