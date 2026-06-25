# Campaign A / A3 retrieval-quality re-measure (closing the decorrelated council's
# caveat: codex + gemma BOTH flagged that I'd measured ingestion plumbing but not
# whether recall@k / the relevance floor survive on the MESSY corpus).
#
# Fair self-retrieval recall, adapted from test/support/live_recall_measure.exs:
# probe = first 10 words of a chunk, VERBATIM (title words not stripped, so the
# title baseline gets every signal it uses); relevant node = that chunk's node.
# PRIVACY-SAFE: probe text is used as a query but NEVER printed — only aggregate
# recall@k / MRR numbers are emitted. Run once per scope to compare clean-prose
# (public/Wikipedia) vs messy-org (group/Confluence+MediaWiki).
#
#   RECALL_SCOPES=group SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/recall_by_scope.exs

require Logger
Logger.configure(level: :warning)

alias Swarm.Core
alias Swarm.Graph.Retrieval
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

k = String.to_integer(System.get_env("RECALL_K", "5"))
scopes = String.split(System.get_env("RECALL_SCOPES", "public"), ",", trim: true)
sample = String.to_integer(System.get_env("RECALL_SAMPLE", "150"))

# Sample chunks whose node is in the target scope (self-retrieval ground truth).
%{rows: rows} =
  Repo.query!(
    """
    SELECT k.node_id, k.text
    FROM chunk k JOIN node n ON n.id = k.node_id
    WHERE length(k.text) > 80 AND n.scope = ANY($1)
    ORDER BY k.node_id, k.ordinal
    """,
    [scopes]
  )

probe_of = fn text ->
  words = String.split(text, ~r/\s+/, trim: true)
  if length(words) >= 6, do: words |> Enum.take(10) |> Enum.join(" "), else: nil
end

probes =
  rows
  |> Enum.map(fn [node_id, text] -> {node_id, probe_of.(text)} end)
  |> Enum.reject(fn {_id, p} -> is_nil(p) end)
  |> Enum.uniq_by(fn {_id, p} -> p end)
  |> Enum.take(sample)

rank_of = fn ids, node_id -> Enum.find_index(ids, &(&1 == node_id)) end
title_ids = fn phrase -> Core.search(phrase, scopes, limit: k) |> Enum.map(& &1.id) end

lex_w = String.to_float(System.get_env("LEX_W", "1.0"))
dense_w = String.to_float(System.get_env("DENSE_W", "1.0"))

retr_ids = fn phrase, dense? ->
  %{memories: m, expanded: e} =
    Retrieval.search(phrase, scopes, limit: k, dense: dense?, max_depth: 1, lex_weight: lex_w, dense_weight: dense_w)

  Enum.map(m, & &1.node_id) ++ Enum.map(e, & &1.id)
end

# answerability = fraction of probes for which the hybrid retriever returns ANY hit
# (above the relevance floor) — the floor's behaviour on this corpus.
score = fn ids_fun ->
  Enum.reduce(probes, {0, 0.0, 0}, fn {node_id, phrase}, {hits, mrr, answered} ->
    ids = ids_fun.(phrase)
    answered = if ids == [], do: answered, else: answered + 1

    case rank_of.(ids, node_id) do
      nil -> {hits, mrr, answered}
      r when r < k -> {hits + 1, mrr + 1.0 / (r + 1), answered}
      _ -> {hits, mrr, answered}
    end
  end)
end

n = length(probes)
pct = fn x -> if n > 0, do: Float.round(x * 100 / n, 1), else: 0.0 end
avg = fn x -> if n > 0, do: Float.round(x / n, 3), else: 0.0 end

{tb, tm, _} = score.(fn p -> title_ids.(p) end)
{lb, lm, la} = score.(fn p -> retr_ids.(p, false) end)
{hb, hm, ha} = score.(fn p -> retr_ids.(p, true) end)

IO.puts("== Recall re-measure — scope=#{inspect(scopes)} (k=#{k}) ==")
IO.puts("probes: #{n} verbatim chunk phrases (privacy-safe: text not printed)\n")
IO.puts("                                   recall@#{k}        MRR@#{k}   answerable")
IO.puts("1. title-ILIKE  (Core.search)      #{pct.(tb)}%  (#{tb}/#{n})    #{avg.(tm)}     —")
IO.puts("2. lexical-only (chunk tsvector)   #{pct.(lb)}%  (#{lb}/#{n})    #{avg.(lm)}     #{pct.(la)}%")
IO.puts("3. hybrid       (lexical ∥ dense)  #{pct.(hb)}%  (#{hb}/#{n})    #{avg.(hm)}     #{pct.(ha)}%")
IO.puts("\ncontent retrieval vs title baseline: +#{Float.round(pct.(hb) - pct.(tb), 1)} pp recall@#{k}")
