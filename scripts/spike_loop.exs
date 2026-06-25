# Cognitive-activation spike — STIGMERGY: activate a SECOND reactor and watch the
# worker→graph→worker loop (card bullet #2: converge or thrash?). Registers a disposable
# `Spike.Enricher` reactor alongside the live `Embedder`, both on `content_added`, behind the
# REAL Dispatch+Tailer. Seeds ONE group article's signal, drains, and traces the cascade.
#
# Convergence by design: the enricher SKIPS non-article nodes, so entities it mints (which get
# a body → content_added → Embedder) do NOT re-trigger enrichment. Hard backstop: a global
# LLM-call cap (cost-asymmetry guard) refuses runaway. All writes are spike state → wiped.
#
# KNOWN LIMITATION (see board/journal.md 2026-06-25): the real Tailer drains the WHOLE outbox
# backlog, not just the seed (here: 564 historical content_added → 564 enrich triggers, 556 refused
# by the budget cap), and the settle-heuristic is fooled by the ~130 s LLM latency (idle ≠ done).
# So this harness DEMONSTRATES the blanket-reactor failure mode + budget containment, but NOT a
# clean convergence observation — for that use spike_converge.exs (deterministic, no LLM). Both
# findings are real; this script is kept as the apparatus that surfaced them.
#
#   SEED_SCOPE=group MAXCALLS=8 SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/spike_loop.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Repo
alias Swarm.Graph.Store
alias Swarm.Ingest.{Content, Embedder}
alias Swarm.Stigmergy.{Dispatch, Tailer}
alias Swarm.ML.Generation

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

# trace + backstop counter
{:ok, _} = Agent.start_link(fn -> %{trace: [], calls: 0} end, name: :spike_trace)
log = fn ev -> Agent.update(:spike_trace, fn s -> %{s | trace: [ev | s.trace]} end) end
bump = fn -> Agent.get_and_update(:spike_trace, fn s -> {s.calls, %{s | calls: s.calls + 1}} end) end
maxcalls = String.to_integer(System.get_env("MAXCALLS", "8"))
scope = System.get_env("SEED_SCOPE", "group")

defmodule SpikeEnr do
  @sys "Extract factual subject-predicate-object claims. Predicate lowercase snake_case. " <>
         "STRICT JSON only: {\"claims\":[{\"s\":\"\",\"p\":\"\",\"o\":\"\"}]}. Max 6 claims."

  def run(node_id, scope, maxcalls, log, bump) do
    case Repo.query!("SELECT type FROM node WHERE id=$1", [node_id]) do
      %{rows: [["article"]]} ->
        n = bump.()

        if n >= maxcalls do
          log.({:refused_budget, node_id})
        else
          enrich(node_id, scope, log)
        end

      %{rows: [[t]]} ->
        # the convergence guard: enrichment output (entities) does NOT re-enrich.
        log.({:skip_nonarticle, node_id, t})

      _ ->
        :noop
    end
  end

  defp enrich(node_id, scope, log) do
    body =
      case Repo.query!("SELECT body FROM content WHERE node_id=$1", [node_id]) do
        %{rows: [[b]]} -> b
        _ -> nil
      end

    claims = if body, do: extract(body), else: []
    log.({:enriched_article, node_id, length(claims)})

    Enum.each(claims, fn %{s: s, p: p, o: o} ->
      subj = Store.upsert_node("entity", key(s), scope: scope)
      obj = Store.upsert_node("entity", key(o), scope: scope)
      Store.add_edge(subj, obj, p, "spike:loop:#{node_id}", scope: scope, reliability: 0.3)
      # give the entity a body → emits content_added → the loop's next generation.
      Content.put_body(subj, s)
      Content.put_body(obj, o)
    end)
  end

  defp extract(body) do
    case Generation.generate("qwen3:14b", "/no_think\nPASSAGE:\n#{String.slice(body, 0, 1800)}\n\nJSON:", json: false, system: @sys) do
      {:ok, raw} ->
        j = case {:binary.match(raw, "{"), :binary.matches(raw, "}")} do
          {{a, _}, ms} when ms != [] -> :binary.part(raw, a, (ms |> List.last() |> elem(0)) - a + 1)
          _ -> "{}"
        end
        case Jason.decode(j) do
          {:ok, %{"claims" => cs}} when is_list(cs) ->
            Enum.flat_map(cs, fn
              %{"s" => s, "p" => p, "o" => o} when is_binary(s) and is_binary(o) and s != "" and o != "" ->
                [%{s: String.slice(String.trim(s), 0, 120), p: String.replace(String.downcase(p), ~r/[^a-z0-9]+/, "_") |> String.trim("_") |> String.slice(0, 50), o: String.slice(String.trim(o), 0, 120)}]
              _ -> []
            end)
          _ -> []
        end
      _ -> []
    end
  end

  defp key(s), do: s |> String.downcase() |> String.replace(~r/\s+/, " ") |> String.trim() |> String.slice(0, 200)
end

# wrap the enricher as a content_added handler fun, closing over log/bump.
enricher = fn %{payload: %{"node_id" => id}} -> SpikeEnr.run(id, scope, maxcalls, log, bump) end

{:ok, _disp} = Dispatch.start_link(subscriptions: [{"content_added", Embedder}, {"content_added", enricher}])
{:ok, _tail} = Tailer.start_link(handler: &Dispatch.dispatch/1, poll_ms: 500)
IO.puts("== STIGMERGY LOOP — 2 reactors on content_added (Embedder + Spike.Enricher), seed scope=#{scope} ==")

# seed: one group article with content → emit content_added.
%{rows: [[seed]]} =
  Repo.query!("SELECT c.node_id FROM content c JOIN node n ON n.id=c.node_id WHERE n.scope=$1 AND length(c.body)>500 ORDER BY c.node_id LIMIT 1", [scope])

Repo.query!(
  "INSERT INTO outbox (change, target_key, payload, idem_key) VALUES ('content_added',$1,$2::jsonb,$3)",
  ["node:#{seed}", Jason.encode!(%{node_id: seed}), "spike-loop-seed:#{seed}"]
)
Repo.query!("SELECT pg_notify('stigmergy','')")
IO.puts("seeded article node #{seed}")

# drive + settle: drain, let async lanes (incl. the 138s enrich) run, until the trace stabilizes.
settle = fn settle ->
  fn prev, stable, elapsed ->
    Tailer.drain()
    Process.sleep(2000)
    %{trace: tr, calls: c} = Agent.get(:spike_trace, & &1)
    now = length(tr)

    cond do
      elapsed > 320 -> {:timeout, tr, c}
      now == prev and stable >= 3 -> {:settled, tr, c}
      now == prev -> settle.(settle).(prev, stable + 1, elapsed + 2)
      true -> settle.(settle).(now, 0, elapsed + 2)
    end
  end
end

{status, trace, calls} = settle.(settle).(-1, 0, 0)

IO.puts("\n-- loop outcome: #{status} (LLM enrich calls: #{calls}) --")
trace
|> Enum.reverse()
|> Enum.frequencies_by(fn
  {:enriched_article, _, n} -> "enriched_article(#{n} claims)"
  {:skip_nonarticle, _, t} -> "enrich-SKIP #{t}"
  {:refused_budget, _} -> "refused(budget)"
  other -> inspect(elem(other, 0))
end)
|> Enum.each(fn {k, v} -> IO.puts("    #{k}: #{v}") end)

art = Enum.count(trace, &match?({:enriched_article, _, _}, &1))
skips = Enum.count(trace, &match?({:skip_nonarticle, _, _}, &1))
IO.puts("\nCONVERGENCE: #{art} article(s) enriched → entities minted → #{skips} entity content_added events were enrich-SKIPPED (no gen-2). Loop #{if status == :settled, do: "CONVERGED", else: status}.")
