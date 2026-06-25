# Card 7 keystone diagnostic (the council's gate): does the DENSE arm earn its place
# on PARAPHRASE queries? Verbatim probes are lexical-biased by construction; here each
# probe is an NL question GENERATED LOCALLY (ollama, offline — intranet content never
# leaves the box, never printed) from a chunk, then we compare lexical-only vs hybrid
# recall@k/MRR. If hybrid >> lexical on paraphrase, dense is worth its ranking cost and
# the fix is to gate it (stop demoting exact hits); if hybrid ≈ lexical, dense is net
# noise. Privacy-safe: only aggregate numbers print.
#
#   RECALL_SCOPES=group PARA_SAMPLE=60 SWARM_DB_NAME=swarm_slice \
#     SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev mise exec -- mix run --no-start \
#     ../../hive/scripts/paraphrase_recall.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Graph.Retrieval
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Application.ensure_all_started(:inets)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

k = String.to_integer(System.get_env("RECALL_K", "5"))
scopes = String.split(System.get_env("RECALL_SCOPES", "group"), ",", trim: true)
sample = String.to_integer(System.get_env("PARA_SAMPLE", "60"))
model = System.get_env("PARA_MODEL", "gemma4:e2b")

# Sample prose-bearing chunks (skip pure code/table — paraphrasing those is noise).
%{rows: rows} =
  Repo.query!(
    "SELECT k.node_id, k.text FROM chunk k JOIN node n ON n.id=k.node_id " <>
      "WHERE n.scope=ANY($1) AND length(k.text)>200 AND k.text !~ '```' AND k.text !~ '\\|[^\\n]*\\|' " <>
      "ORDER BY k.node_id, k.ordinal",
    [scopes]
  )

sampled = rows |> Enum.take_every(max(1, div(length(rows), sample))) |> Enum.take(sample)

# Generate ONE natural question from a chunk via the local model (offline).
gen_question = fn text ->
  prompt =
    "Read the passage and write ONE short, natural question that the passage answers. " <>
      "Use DIFFERENT words than the passage where you can. Output only the question, nothing else.\n\nPASSAGE:\n" <>
      String.slice(text, 0, 1200) <> "\n\nQUESTION:"

  body = JSON.encode!(%{model: model, prompt: prompt, stream: false, options: %{temperature: 0.3}})

  case :httpc.request(:post, {~c"http://localhost:11434/api/generate", [], ~c"application/json", body}, [timeout: 60_000], body_format: :binary) do
    {:ok, {{_, 200, _}, _, resp}} ->
      case JSON.decode(resp) do
        {:ok, %{"response" => q}} -> q |> String.trim() |> String.split("\n") |> List.first()
        _ -> nil
      end

    _ ->
      nil
  end
end

lex_w = String.to_float(System.get_env("LEX_W", "1.0"))
dense_w = String.to_float(System.get_env("DENSE_W", "1.0"))

ids = fn phrase, dense? ->
  %{memories: m, expanded: e} =
    Retrieval.search(phrase, scopes, limit: k, dense: dense?, max_depth: 1, lex_weight: lex_w, dense_weight: dense_w)

  Enum.map(m, & &1.node_id) ++ Enum.map(e, & &1.id)
end

rank = fn list, node -> Enum.find_index(Enum.take(list, k), &(&1 == node)) end

acc =
  Enum.reduce(sampled, %{n: 0, lex_h: 0, lex_mrr: 0.0, hyb_h: 0, hyb_mrr: 0.0}, fn [node, text], a ->
    case gen_question.(text) do
      q when is_binary(q) and byte_size(q) > 5 ->
        lr = rank.(ids.(q, false), node)
        hr = rank.(ids.(q, true), node)

        %{
          a
          | n: a.n + 1,
            lex_h: a.lex_h + if(lr, do: 1, else: 0),
            lex_mrr: a.lex_mrr + if(lr, do: 1.0 / (lr + 1), else: 0.0),
            hyb_h: a.hyb_h + if(hr, do: 1, else: 0),
            hyb_mrr: a.hyb_mrr + if(hr, do: 1.0 / (hr + 1), else: 0.0)
        }

      _ ->
        a
    end
  end)

n = acc.n
pct = fn x -> if n > 0, do: Float.round(x * 100 / n, 1), else: 0.0 end
avg = fn x -> if n > 0, do: Float.round(x / n, 3), else: 0.0 end

IO.puts("== Paraphrase recall — scope=#{inspect(scopes)} (k=#{k}, n=#{n}, gen=#{model}) ==")
IO.puts("probes: locally-generated NL questions (privacy-safe: not printed)\n")
IO.puts("                  recall@#{k}        MRR@#{k}")
IO.puts("lexical-only      #{pct.(acc.lex_h)}%  (#{acc.lex_h}/#{n})    #{avg.(acc.lex_mrr)}")
IO.puts("hybrid (+dense)   #{pct.(acc.hyb_h)}%  (#{acc.hyb_h}/#{n})    #{avg.(acc.hyb_mrr)}")
IO.puts("\ndense arm's paraphrase lift: recall +#{Float.round(pct.(acc.hyb_h) - pct.(acc.lex_h), 1)} pp, " <>
  "MRR +#{Float.round(avg.(acc.hyb_mrr) - avg.(acc.lex_mrr), 3)}")
