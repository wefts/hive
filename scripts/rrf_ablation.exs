# Card 6 ship-gate ablation (decorrelated council demand, codex+gemma): PROVE the
# hybrid recall@5 dip on the structured group slice is dense-fusion DEMOTION of exact
# lexical hits, not chunks gone missing/mis-scoped from segmentation. Per probe,
# compare lexical-only vs hybrid top-k membership of the source node. Privacy-safe.
#
#   RECALL_SCOPES=group SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/rrf_ablation.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Graph.Retrieval
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

k = String.to_integer(System.get_env("RECALL_K", "5"))
scopes = String.split(System.get_env("RECALL_SCOPES", "group"), ",", trim: true)
sample = String.to_integer(System.get_env("RECALL_SAMPLE", "150"))

%{rows: rows} =
  Repo.query!(
    "SELECT k.node_id, k.text FROM chunk k JOIN node n ON n.id=k.node_id WHERE length(k.text)>80 AND n.scope=ANY($1) ORDER BY k.node_id, k.ordinal",
    [scopes]
  )

probes =
  rows
  |> Enum.map(fn [id, t] -> {id, t |> String.split(~r/\s+/, trim: true) |> Enum.take(10) |> Enum.join(" ")} end)
  |> Enum.uniq_by(&elem(&1, 1))
  |> Enum.take(sample)

ids = fn phrase, dense? ->
  %{memories: m, expanded: e} = Retrieval.search(phrase, scopes, limit: k, dense: dense?, max_depth: 1)
  Enum.map(m, & &1.node_id) ++ Enum.map(e, & &1.id)
end

in_topk? = fn list, node -> node in Enum.take(list, k) end

# classify each probe by (lexical hit?, hybrid hit?)
tally =
  Enum.reduce(probes, %{both: 0, lex_only: 0, hyb_only: 0, neither: 0}, fn {node, phrase}, acc ->
    lex = in_topk?.(ids.(phrase, false), node)
    hyb = in_topk?.(ids.(phrase, true), node)

    key =
      cond do
        lex and hyb -> :both
        lex and not hyb -> :lex_only
        hyb and not lex -> :hyb_only
        true -> :neither
      end

    Map.update!(acc, key, &(&1 + 1))
  end)

n = length(probes)
hybrid_misses = tally.lex_only + tally.neither

IO.puts("== RRF ablation — scope=#{inspect(scopes)} (k=#{k}, n=#{n}) ==")
IO.puts("both lexical+hybrid found:        #{tally.both}")
IO.puts("lexical found, HYBRID MISSED:     #{tally.lex_only}   <- dense-fusion DEMOTION")
IO.puts("hybrid found, lexical missed:     #{tally.hyb_only}")
IO.puts("neither found:                    #{tally.neither}   <- genuine miss (chunk gone/mis-scoped)")
IO.puts("")
IO.puts("hybrid misses total: #{hybrid_misses}")

verdict =
  cond do
    hybrid_misses == 0 -> "no hybrid misses"
    tally.neither == 0 -> "ALL hybrid misses are lexical-hits → 100% dense-fusion demotion, NOT segmentation loss"
    true -> "#{tally.lex_only}/#{hybrid_misses} hybrid misses are demotions; #{tally.neither} are genuine (segmentation/scope)"
  end

IO.puts("VERDICT: #{verdict}")
