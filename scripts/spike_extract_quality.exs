# Cognitive-activation spike — Step 1: extraction quality (PUBLIC scope only, printable).
# Does the local fleet, driven through the kernel gRPC boundary, extract USEFUL
# subject-predicate-object claims from real content? Tune the prompt here on
# Wikipedia (public) where claim text is safe to print; the group-scope activation
# (later steps) reuses this extractor but prints aggregate numbers only.
#
#   SAMPLE=8 MODEL=qwen3:14b SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/spike_extract_quality.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Repo
alias Swarm.ML.Generation

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

model = System.get_env("MODEL", "qwen3:14b")
sample = String.to_integer(System.get_env("SAMPLE", "8"))
scope = System.get_env("SCOPE", "public")
printable = scope == "public"

defmodule Spike.Extract do
  @system "You extract factual claims from a passage as subject-predicate-object triples. " <>
            "Extract ONLY claims explicitly stated in the passage. The predicate is a short " <>
            "lowercase snake_case verb phrase (e.g. located_in, founded_by, is_a, part_of). " <>
            "Keep subject/object short noun phrases. Output STRICT JSON only, no prose: " <>
            "{\"claims\":[{\"s\":\"subject\",\"p\":\"predicate\",\"o\":\"object\"}]}. At most 8 claims."

  @spec claims(String.t(), String.t()) :: {:ok, [map()], integer()} | {:error, term()}
  def claims(body, model) do
    prompt = "PASSAGE:\n" <> String.slice(body, 0, 2400) <> "\n\nJSON:"
    t0 = System.monotonic_time(:millisecond)

    # NB: ollama json-mode (json: true) pretty-prints + truncates → invalid JSON.
    # json: false with the strict-JSON system prompt returns clean compact JSON.
    case Generation.generate(model, prompt, json: false, system: @system) do
      {:ok, raw} ->
        dt = System.monotonic_time(:millisecond) - t0
        {:ok, parse(raw), dt}

      {:error, _} = e ->
        e
    end
  end

  # Robust: slice the first '{' .. last '}' (drops any stray prose/think tokens).
  defp parse(raw) do
    json =
      case {:binary.match(raw, "{"), :binary.match(raw, "}")} do
        {{a, _}, _} ->
          last = raw |> :binary.matches("}") |> List.last() |> elem(0)
          :binary.part(raw, a, last - a + 1)

        _ ->
          raw
      end

    case Jason.decode(json) do
      {:ok, %{"claims" => cs}} when is_list(cs) ->
        Enum.flat_map(cs, fn
          %{"s" => s, "p" => p, "o" => o}
          when is_binary(s) and is_binary(p) and is_binary(o) and s != "" and o != "" ->
            [%{s: String.trim(s), p: String.trim(p), o: String.trim(o)}]

          _ ->
            []
        end)

      _ ->
        []
    end
  end
end

%{rows: rows} =
  Repo.query!(
    "SELECT c.node_id, c.body FROM content c JOIN node n ON n.id = c.node_id " <>
      "WHERE n.scope = $1 AND length(c.body) > 300 ORDER BY c.node_id",
    [scope]
  )

sampled = rows |> Enum.take_every(max(1, div(length(rows), sample))) |> Enum.take(sample)

{tot_claims, tot_ms, parse_fail, preds} =
  Enum.reduce(sampled, {0, 0, 0, %{}}, fn [node_id, body], {tc, tms, pf, preds} ->
    case Spike.Extract.claims(body, model) do
      {:ok, claims, dt} ->
        if printable do
          IO.puts("\n— node #{node_id}: #{length(claims)} claims in #{dt}ms")
          Enum.each(claims, fn c -> IO.puts("    (#{c.s}) -[#{c.p}]-> (#{c.o})") end)
        end

        preds = Enum.reduce(claims, preds, fn c, m -> Map.update(m, c.p, 1, &(&1 + 1)) end)
        pf = if claims == [], do: pf + 1, else: pf
        {tc + length(claims), tms + dt, pf, preds}

      {:error, r} ->
        IO.puts("  node #{node_id}: ERROR #{inspect(r)}")
        {tc, tms, pf + 1, preds}
    end
  end)

n = length(sampled)
IO.puts("\n== Extraction quality — scope=#{scope}, model=#{model}, n=#{n} ==")
IO.puts("total claims:        #{tot_claims}  (avg #{Float.round(tot_claims / max(n, 1), 1)}/node)")
IO.puts("empty/parse-fail:    #{parse_fail}/#{n} nodes")
IO.puts("avg latency:         #{div(tot_ms, max(n, 1))} ms/node")
IO.puts("distinct predicates: #{map_size(preds)}  (#{tot_claims} claims)")
