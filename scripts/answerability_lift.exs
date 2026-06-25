# Answerability-lift harness (CTC-3) — does the cognitive layer improve answers?
# READ-ONLY. AGGREGATE output only by default (rates/means/lift), never retrieved
# keys — on the real corpus those keys ARE content. Per-query detail (counts, no
# keys) only under VERBOSE=1, intended for PUBLIC scope.
#
#   QUERY_SET=/path/to/qa.json SCOPES=public RECALL_K=10 \
#     SWARM_DB_NAME=swarm_shadow MIX_ENV=dev \
#     mise exec -- mix run --no-start ../../hive/scripts/answerability_lift.exs
#
# qa.json: [{"q": "question text", "gold": ["node_key1", "node_key2"]}, ...]
# gold = the node keys a correct answer should cite (the OPERATOR's labeled set —
# external, never committed; privacy).
#
# Two comparable lift axes:
#  (A) QUERY-MODE (this run, one DB): retrieval-only (expand:false) vs with-cognition
#      (expand:true → traverse-relaxation reaches claim/folded-entity neighbours).
#  (B) CORPUS axis (the epic's real question): run this on the CLEAN pre-loop seed
#      snapshot AND the post-hot-run snapshot; the operator diffs the two blocks.
# This script computes axis A directly and the absolute block the operator diffs for
# axis B. Calibrate/measure on the operator's REAL corpus + labeled set — a public
# shadow with 0 claims shows ~0 query-mode lift (mechanics check, not a result).

require Logger
Logger.configure(level: :warning)
alias Swarm.Graph.Retrieval
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()

# The dense arm embeds via the ML gRPC sidecar; under `mix run --no-start` the
# client supervisor isn't in the tree, so start it. If the sidecar is unreachable
# the embed degrades to lexical-only (search handles a nil query vector).
case DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor) do
  {:ok, _} -> :ok
  {:error, {:already_started, _}} -> :ok
end

set_path = System.get_env("QUERY_SET")
scopes = System.get_env("SCOPES", "public") |> String.split(",", trim: true)
k = System.get_env("RECALL_K", "10") |> String.to_integer()
verbose = System.get_env("VERBOSE") == "1"

unless set_path && File.exists?(set_path) do
  IO.puts("""
  answerability-lift: set QUERY_SET to a JSON labeled set the operator provides:
    [{"q": "question", "gold": ["node_key", ...]}, ...]
  Aggregate output only (privacy); calibrate on the real corpus, not synthetic.
  """)

  System.halt(0)
end

queries = set_path |> File.read!() |> Jason.decode!()
IO.puts("== answerability lift — #{length(queries)} queries, scopes=#{inspect(scopes)}, recall@#{k} ==\n")

# Resolve expanded node ids → keys (memories already carry :key).
keys_for = fn ids ->
  case ids do
    [] -> %{}
    _ -> Repo.query!("SELECT id, key FROM node WHERE id = ANY($1)", [ids]).rows |> Map.new(fn [i, key] -> {i, key} end)
  end
end

hits = fn keys, gold -> length(keys -- (keys -- gold)) end

# Council (codex + gemma, decorrelated): do NOT merge retrieval-score memories and
# traversal-confidence expansions into one capped ranking — the scales are
# incommensurable and the merge would reward/punish traversal by score-scale
# artifact, not usefulness. Instead exploit the structure: in this query-mode A/B
# the MEMORIES are identical across arms (expand only ADDS nodes), so report
# memory-recall (the shared retrieval baseline) + traversal's MARGINAL recall (gold
# surfaced ONLY via expansion, beyond memories). One search/query (expand:true).
acc =
  Enum.reduce(queries, %{n: 0, found: 0, gold_n: 0, mem_r: 0.0, cov_r: 0.0, mem_sum: 0, exp_sum: 0}, fn %{"q" => q} = item, a ->
    gold = Map.get(item, "gold", [])
    res = Retrieval.search(q, scopes, limit: k, expand: true)

    mem_keys = res.memories |> Enum.map(& &1.key) |> Enum.take(k)
    idmap = keys_for.(Enum.map(res.expanded, & &1.id))
    exp_keys = res.expanded |> Enum.map(&Map.get(idmap, &1.id)) |> Enum.reject(&is_nil/1)
    cov_keys = Enum.uniq(mem_keys ++ exp_keys)

    {mem_r, cov_r, labeled} =
      if gold == [], do: {0.0, 0.0, 0}, else: {hits.(mem_keys, gold) / length(gold), hits.(cov_keys, gold) / length(gold), 1}

    if verbose do
      IO.puts("  q: #{String.slice(q, 0, 44)} | mem=#{length(res.memories)} exp=#{length(res.expanded)} mem_recall=#{Float.round(mem_r, 2)} +traversal=#{Float.round(cov_r - mem_r, 2)}")
    end

    %{
      a
      | n: a.n + 1,
        found: a.found + if(res.status == :found, do: 1, else: 0),
        gold_n: a.gold_n + labeled,
        mem_r: a.mem_r + mem_r,
        cov_r: a.cov_r + cov_r,
        mem_sum: a.mem_sum + length(res.memories),
        exp_sum: a.exp_sum + length(res.expanded)
    }
  end)

rate = fn x, n -> if n > 0, do: Float.round(x / n, 3), else: 0.0 end

IO.puts("")
IO.puts("  retrieval (memories — shared baseline; identical in both arms)")
IO.puts("    answerability:        #{rate.(acc.found, acc.n)}  (#{acc.found}/#{acc.n} status=found)")
IO.puts("    memory recall@#{k}:      #{rate.(acc.mem_r, acc.gold_n)}  (over #{acc.gold_n} labeled queries)")
IO.puts("    mean memories:        #{rate.(acc.mem_sum, acc.n)}")
IO.puts("")
IO.puts("  + traversal (expand:true — adds claim/folded-entity neighbours)")
IO.puts("    mean expanded:        #{rate.(acc.exp_sum, acc.n)}  (neighbours reached)")
IO.puts("    coverage recall@#{k}:    #{rate.(acc.cov_r, acc.gold_n)}  (memories ∪ traversal)")
IO.puts("    TRAVERSAL recall lift: #{rate.(acc.cov_r - acc.mem_r, acc.gold_n)}  (gold surfaced ONLY via traversal)")
IO.puts("")
IO.puts("  NB: this is TRAVERSAL lift (part 3 of the cognitive layer) — claims + entity")
IO.puts("  folding (parts 1,2) are baked into the graph and help BOTH arms equally.")
IO.puts("  FULL cognition lift = diff the memory-recall block across the pre-loop vs")
IO.puts("  post-loop corpus snapshots (the operator's two-snapshot run).")
