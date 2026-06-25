# Cognitive-activation spike — ACTIVATE enrichment on the live slice (GUARDED, disposable).
# Reads source nodes' content, LLM-extracts subject-predicate-object claims, writes them into
# the live graph as `entity` nodes + typed claim relation edges with provenance lineage and a
# HARD single-source confidence cap (D3). Records ONLY aggregate numbers (group = intranet).
#
# Wipeable by construction: pre-slice has ONLY type='article' nodes + ONLY links_to/child_of
# edges, so any non-article node / other-typed edge is spike-created (also tagged spike:*).
# Corroboration/node.vec/traversal measurement: see spike_measure.exs (fast, over the result).
#
#   SAMPLE=30 SCOPE=group MODEL=qwen3:14b CAP=0.3 SWARM_DB_NAME=swarm_slice \
#     SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev mise exec -- mix run --no-start \
#     ../../hive/scripts/spike_activate.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Repo
alias Swarm.Graph.Store
alias Swarm.ML.Generation

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

defmodule H do
  def norm(s), do: s |> String.trim() |> String.replace(~r/\s+/, " ") |> String.slice(0, 200)
  def normp(p), do: p |> String.downcase() |> String.replace(~r/[^a-z0-9]+/, "_") |> String.trim("_") |> String.slice(0, 60)
  def keyname(s), do: s |> String.downcase() |> String.replace(~r/\s+/, " ") |> String.trim() |> String.slice(0, 200)

  @sys "You extract factual claims from a passage as subject-predicate-object triples. " <>
         "Extract ONLY claims explicitly stated. Predicate is a short lowercase snake_case verb " <>
         "phrase (located_in, founded_by, is_a, part_of, reports_to, owns). Keep subject/object " <>
         "short noun phrases. Output STRICT JSON only: " <>
         "{\"claims\":[{\"s\":\"\",\"p\":\"\",\"o\":\"\"}]}. At most 8 claims."

  def extract(body, model) do
    prompt = "/no_think\nPASSAGE:\n" <> String.slice(body, 0, 2400) <> "\n\nJSON:"

    case Generation.generate(model, prompt, json: false, system: @sys) do
      {:ok, raw} -> parse(raw)
      {:error, _} -> []
    end
  end

  defp parse(raw) do
    json =
      case {:binary.match(raw, "{"), :binary.matches(raw, "}")} do
        {{a, _}, ms} when ms != [] -> :binary.part(raw, a, (ms |> List.last() |> elem(0)) - a + 1)
        _ -> "{}"
      end

    case Jason.decode(json) do
      {:ok, %{"claims" => cs}} when is_list(cs) ->
        Enum.flat_map(cs, fn
          %{"s" => s, "p" => p, "o" => o}
          when is_binary(s) and is_binary(p) and is_binary(o) and s != "" and o != "" ->
            [%{s: norm(s), p: normp(p), o: norm(o)}]

          _ ->
            []
        end)

      _ ->
        []
    end
  end
end

model = System.get_env("MODEL", "qwen3:14b")
sample = String.to_integer(System.get_env("SAMPLE", "30"))
scope = System.get_env("SCOPE", "group")
cap = String.to_float(System.get_env("CAP", "0.3"))
printable = scope == "public"

%{rows: rows} =
  Repo.query!(
    "SELECT c.node_id, c.body FROM content c JOIN node n ON n.id=c.node_id " <>
      "WHERE n.scope=$1 AND length(c.body)>300 ORDER BY c.node_id",
    [scope]
  )

sources = rows |> Enum.take_every(max(1, div(length(rows), sample))) |> Enum.take(sample)
IO.puts("== ACTIVATION — scope=#{scope}, model=#{model}, sources=#{length(sources)}, cap=#{cap} ==")

state =
  Enum.reduce(Enum.with_index(sources), %{claims: 0, edges_new: 0, ent: MapSet.new(), ms: 0, empty: 0}, fn {[src_id, body], i}, acc ->
    t0 = System.monotonic_time(:millisecond)
    claims = H.extract(body, model)
    dt = System.monotonic_time(:millisecond) - t0
    prov = "spike:src:#{src_id}"

    acc =
      Enum.reduce(claims, acc, fn c, a ->
        subj = Store.upsert_node("entity", H.keyname(c.s), scope: scope)
        obj = Store.upsert_node("entity", H.keyname(c.o), scope: scope)

        reinforced? =
          case Store.add_edge(subj, obj, c.p, prov, scope: scope, reliability: cap) do
            {:ok, r} -> r.reinforced
            _ -> false
          end

        %{a | claims: a.claims + 1, edges_new: a.edges_new + if(reinforced?, do: 1, else: 0),
          ent: a.ent |> MapSet.put(H.keyname(c.s)) |> MapSet.put(H.keyname(c.o))}
      end)

    if printable and claims != [], do: IO.puts("src #{src_id}: #{length(claims)} claims #{dt}ms")
    if rem(i + 1, 5) == 0, do: IO.puts("  …#{i + 1}/#{length(sources)} sources, #{acc.claims} claims")
    %{acc | ms: acc.ms + dt, empty: acc.empty + if(claims == [], do: 1, else: 0)}
  end)

n = length(sources)
IO.puts("\n-- enrichment result --")
IO.puts("claims extracted:    #{state.claims}  (avg #{Float.round(state.claims / max(n, 1), 1)}/source, #{state.empty} empty)")
IO.puts("distinct entities:   #{MapSet.size(state.ent)}  (exact-key dedup collapsed repeats)")
IO.puts("new claim edges:     #{state.edges_new}")
IO.puts("extract latency:     #{div(state.ms, max(n, 1))} ms/source avg")
