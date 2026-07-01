# Retrieval title-arm probe (ADR-0016 Phase 1) — READ-ONLY, AGGREGATE output.
#
# Measures whether the title-matched page leads + recall@K / MRR over a labeled
# qa set, for the CURRENT kernel checkout. To compare baseline vs the title arm,
# run it twice — once on `main` (no title arm) and once on the feat branch — and
# diff the aggregate blocks (the probe itself is branch-independent; only
# Swarm.Graph.Retrieval differs between checkouts).
#
# NB: MIX_ENV=prod below is Elixir's own compile-mode env (release-shaped build),
# NOT the app-level SWARM_ENV stage (ADR-0015) — orthogonal axes, same word.
# SWARM_DB_NAME=swarm_prod is today's staging DB (ADR-14); update to
# swarm_staging once the ADR-0015 rename has landed.
#
#   QUERY_SET=/path/to/qa.json SCOPES=group RECALL_K=10 \
#     SWARM_DB_NAME=swarm_prod MIX_ENV=prod \
#     mise exec -- mix run --no-start ../../hive/scripts/retrieval_title_probe.exs
#
# Privacy: the per-query line prints the gold key + its rank (the gold KEYS are
# the operator's labeled titles — keep them in the console/scratchpad, NOT in
# committed artifacts). The aggregate block (recall, MRR, #leads) is the only
# thing safe to journal. No chunk bodies / values are ever printed.

require Logger
Logger.configure(level: :warning)
alias Swarm.Graph.Retrieval
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()

case DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor) do
  {:ok, _} -> :ok
  {:error, {:already_started, _}} -> :ok
end

set_path = System.get_env("QUERY_SET")
scopes = System.get_env("SCOPES", "group") |> String.split(",", trim: true)
k = System.get_env("RECALL_K", "10") |> String.to_integer()

# TITLE_WEIGHT: override the title-arm weight for an A/B. Unset → config default
# (5.0). Set to 0 for the baseline (title arm contributes nothing → body-only).
base_opts = [limit: k, expand: false]
base_opts = if System.get_env("DENSE") == "false", do: [{:dense, false} | base_opts], else: base_opts

search_opts =
  case System.get_env("TITLE_WEIGHT") do
    nil -> base_opts
    tw -> [{:title_weight, String.to_float(tw)} | base_opts]
  end

# LEXICAL_ENGINE=bm25 flips the config flag for this run (native vs bm25 A/B, ADR-0016).
if eng = System.get_env("LEXICAL_ENGINE") do
  prev = Application.get_env(:swarm, :retrieval, [])
  Application.put_env(:swarm, :retrieval, Keyword.put(prev, :lexical_engine, String.to_atom(eng)))
end

unless set_path && File.exists?(set_path) do
  IO.puts("retrieval-title-probe: set QUERY_SET to a JSON labeled set [{\"q\":..,\"gold\":[key,..]}]")
  System.halt(0)
end

queries =
  set_path |> File.read!() |> Jason.decode!() |> Enum.reject(&(&1["excluded"] == true))

tw_label = System.get_env("TITLE_WEIGHT") || "config-default"
IO.puts("== retrieval title-arm probe — #{length(queries)} queries, scopes=#{inspect(scopes)}, recall@#{k}, title_weight=#{tw_label} ==")
IO.puts("   db=#{Repo.query!("SELECT current_database()", []).rows |> hd() |> hd()}\n")

# rank of the FIRST gold key in the ranked memory list (1-based), or nil.
rank_of_gold = fn keys, gold ->
  keys
  |> Enum.with_index(1)
  |> Enum.find_value(fn {key, i} -> if key in gold, do: i, else: nil end)
end

acc =
  Enum.reduce(queries, %{n: 0, labeled: 0, recall: 0.0, rr: 0.0, leads: 0, top_hit: 0}, fn %{"q" => q} = item, a ->
    gold = Map.get(item, "gold", [])
    res = Retrieval.search(q, scopes, search_opts)
    keys = res.memories |> Enum.map(& &1.key) |> Enum.take(k)

    rank = rank_of_gold.(keys, gold)
    in_topk = if rank, do: 1, else: 0
    rr = if rank, do: 1.0 / rank, else: 0.0
    lead = if rank == 1, do: 1, else: 0

    IO.puts(
      "  #{String.pad_trailing(String.slice(q, 0, 42), 42)} | rank=#{rank || "—"} (of #{length(keys)})"
    )

    %{
      a
      | n: a.n + 1,
        labeled: a.labeled + if(gold == [], do: 0, else: 1),
        recall: a.recall + in_topk,
        rr: a.rr + rr,
        leads: a.leads + lead,
        top_hit: a.top_hit + if(length(keys) > 0, do: 1, else: 0)
    }
  end)

rate = fn x, n -> if n > 0, do: Float.round(x / n, 3), else: 0.0 end

IO.puts("\n  -- AGGREGATE (safe to journal) --")
IO.puts("    labeled queries:   #{acc.labeled}")
IO.puts("    recall@#{k}:          #{rate.(acc.recall, acc.labeled)}  (#{acc.recall}/#{acc.labeled} gold in top-#{k})")
IO.puts("    MRR:               #{rate.(acc.rr, acc.labeled)}")
IO.puts("    gold leads (#1):   #{rate.(acc.leads, acc.labeled)}  (#{acc.leads}/#{acc.labeled} gold ranked #1)")
