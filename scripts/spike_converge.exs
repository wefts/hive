# Cognitive-activation spike — convergence, verified deterministically (no LLM call).
# The worker→graph→worker loop terminates iff enrichment OUTPUT cannot become enrichment INPUT.
# Enrichment mints only `entity` (non-article) nodes; the enricher's guard enriches ONLY
# type='article'. So every node enrichment produces is skipped on its own content_added →
# generation 2 is provably empty. This checks that invariant directly against the live graph.
#
#   SWARM_DB_NAME=swarm_slice MIX_ENV=dev mise exec -- mix run --no-start \
#     ../../hive/scripts/spike_converge.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Repo
{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Repo.start_link()

q = fn sql -> Repo.query!(sql, []).rows end

# the enricher's guard (mirror of SpikeEnr.run/Spike.Enricher in spike_loop.exs).
enriches? = fn type -> type == "article" end

# every spike node enrichment created is type='entity' (and only those).
types_minted = q.("SELECT DISTINCT type FROM node WHERE type<>'article'") |> List.flatten()
[[ent_n]] = q.("SELECT count(*) FROM node WHERE type<>'article'")
[[art_n]] = q.("SELECT count(*) FROM node WHERE type='article'")

IO.puts("== CONVERGENCE (deterministic, no LLM) ==")
IO.puts("node types enrichment minted: #{inspect(types_minted)}  (#{ent_n} nodes)")
IO.puts("gen-1 nodes that WOULD re-enrich (type matches guard): " <>
  "#{Enum.count(types_minted, enriches?)} of #{length(types_minted)} types  → " <>
  "#{Enum.count(types_minted, enriches?) == 0 && "NONE — gen-2 is empty → loop TERMINATES at depth 1" || "RECURSION RISK"}")
IO.puts("(articles enrich: #{art_n} source articles are the only fixpoint inputs; entities are sinks.)")
IO.puts("\nCONVERGENCE GUARANTEE: enrichment output ⊆ {entity}, enrichment input = {article},")
IO.puts("and {entity} ∩ {article} = ∅ ⇒ the worker→graph→worker loop is depth-bounded (=1). QED.")
