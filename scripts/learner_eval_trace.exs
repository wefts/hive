# Trace the existing serve path for the learner-eval controls. DIAGNOSIS, NOT A FIX.
#
# The point is to stop guessing which stage loses an inventory fact. For each control
# question this replicates `Swarm.Core.coverage_descriptor/6` exactly -- cheap route
# first, then the semantic-router fallback under the same condition Core uses -- and
# records, per row:
#
#   * cheap route: intent, domain, blockers
#   * semantic route: whether Core would have fallen back, the route it returns, and the
#     descriptor that follows
#   * network candidate keys and the RANK of the expected `net:host:<site>/<name>` key
#   * facts for the expected key at four cuts: no filter at all, after scope, after the
#     corroboration floor, after freshness
#   * Stage-2 verdict from the real gate, and the gate's wall time against the breaker
#
# Then three counterfactuals, each isolating one suspect without touching the code:
#
#   A force :network        -- semantic_route pinned, so routing cannot be the cause
#   B force the expected key -- network_keys pinned, so candidate binding cannot be
#   C bypass Stage 2         -- entail_fun that always returns true, so the veto cannot be
#
# Reads only. Nothing here writes to the graph and nothing here grades.
#
# Usage (compose-derived env, as always):
#   export SWARM_ENV=staging
#   eval "$(hive/scripts/kernel-measure-env)"
#   LEARNER_TRACE_SET=hive/tmp/learner-eval/set_frozen.jsonl \
#     LEARNER_TRACE_OUT=hive/tmp/learner-eval/trace_controls.jsonl \
#     MIX_ENV=dev mise exec -C swarm/kernel -- mix run --no-start hive/scripts/learner_eval_trace.exs

require Logger
Logger.configure(level: :warning)

defmodule LearnerEvalTrace do
  @moduledoc false

  alias Swarm.Graph.Aggregation
  alias Swarm.Graph.Network
  alias Swarm.Graph.Procedure
  alias Swarm.Repo
  alias Swarm.WorldMap.Coverage
  alias Swarm.WorldMap.Domain
  alias Swarm.WorldMap.Gate
  alias Swarm.WorldMap.SemanticRouter

  def run do
    set = require_env!("LEARNER_TRACE_SET")
    out = require_env!("LEARNER_TRACE_OUT")

    {:ok, _} = Application.ensure_all_started(:swarm)
    scopes = scopes()

    rows =
      set
      |> read_jsonl!()
      |> Enum.drop(1)
      |> Enum.filter(&(&1["control_question"] not in [nil, ""]))

    IO.puts("learner-eval trace: #{length(rows)} controls, scopes=#{length(scopes)}")

    traced =
      rows
      |> Enum.with_index(1)
      |> Enum.map(fn {row, i} ->
        t = trace_row(row, scopes)

        IO.puts(
          "  [#{i}/#{length(rows)}] #{row["row_id"]} " <>
            "cheap=#{t.cheap.intent}/#{inspect(t.cheap.blockers)} " <>
            "sem=#{inspect(t.semantic.route)} " <>
            "rank=#{inspect(t.candidates.expected_rank)} " <>
            "facts=#{t.facts.unfiltered}/#{t.facts.after_scope}/#{t.facts.after_corroboration}/#{t.facts.after_freshness} " <>
            "hosted_on_ok=#{t.hosted_on.present} bound=#{inspect(t.binding.bound_key)} match=#{t.binding.matches_expected} " <>
            "stage2=#{inspect(t.gate.stage2)} decision=#{t.gate.decision} #{t.gate.ms}ms" <>
            "  cf: net=#{t.counterfactuals.force_network.decision} " <>
            "key=#{t.counterfactuals.force_key.decision} " <>
            "noveto=#{t.counterfactuals.bypass_stage2.decision}"
        )

        t
      end)

    write_jsonl!(out, [summary(traced, scopes) | traced])
    IO.puts("learner-eval trace: wrote #{out}")
  end

  # --- one row ----------------------------------------------------------------------

  defp trace_row(row, scopes) do
    query = row["control_question"]
    expect = row["expect"] || %{}
    site = row["site"]
    host = expect["host"]
    want_node = expect["node"]
    expected_key = "net:host:#{site}/#{host}"
    expected_node_key = "net:host:#{site}/#{want_node}"

    hits = retrieval_hits(query, scopes)
    profile = Aggregation.entity_profile(query, scopes)

    cheap = descriptor(query, scopes, hits, profile, :cheap)
    fallback? = semantic_fallback?(cheap)

    {route, semantic_desc} =
      if fallback? do
        sem = SemanticRouter.route(query, [])
        {sem[:route] || Map.get(sem, :route), descriptor(query, scopes, hits, profile, sem)}
      else
        {:not_attempted, cheap}
      end

    effective = semantic_desc

    net_keys = network_candidate_keys(query, scopes, [])
    rank = Enum.find_index(net_keys, &(&1 == expected_key))

    {gate_decision, gate_audit, gate_ms} = run_gate(effective, scopes, [])

    %{
      kind: "learner_eval_trace",
      row_id: row["row_id"],
      shape: row["shape"],
      site: site,
      host: host,
      expected_node: want_node,
      expected_key: expected_key,
      expected_node_key: expected_node_key,
      control_question: query,
      retrieval_hit_keys: Enum.map(hits, & &1.key) |> Enum.take(8),
      cheap: describe_summary(cheap),
      semantic: %{
        would_fall_back: fallback?,
        route: route,
        descriptor: describe_summary(semantic_desc)
      },
      candidates: %{
        network_key_count: length(net_keys),
        network_keys_top: Enum.take(net_keys, 8),
        expected_rank: rank,
        expected_present: rank != nil
      },
      facts: fact_cuts(expected_key, scopes),
      hosted_on: hosted_on_check(expected_key, expected_node_key, scopes),
      binding: binding_check(effective, expected_key),
      gate: %{
        decision: gate_decision,
        intent: gate_audit && gate_audit.intent,
        blockers: (gate_audit && gate_audit.blockers) || [],
        stage2: gate_audit && gate_audit.stage2,
        ms: gate_ms,
        breaker_ms: breaker_ms(),
        broke_breaker: gate_ms >= breaker_ms()
      },
      counterfactuals: %{
        force_network: counterfactual_force_network(query, scopes, hits, profile),
        force_key: counterfactual_force_key(query, scopes, hits, profile, expected_key),
        bypass_stage2: counterfactual_bypass_stage2(effective, scopes)
      }
    }
  end

  # --- the three counterfactuals ------------------------------------------------------

  # A. Routing cannot be the cause: pin the semantic route to the network domain.
  defp counterfactual_force_network(query, scopes, hits, profile) do
    desc = descriptor(query, scopes, hits, profile, %{route: {:neighborhood, :network}})
    {decision, audit, ms} = run_gate(desc, scopes, [])
    %{decision: decision, intent: audit && audit.intent, blockers: (audit && audit.blockers) || [],
      stage2: audit && audit.stage2, ms: ms, descriptor: describe_summary(desc)}
  end

  # B. Candidate binding cannot be the cause: hand the gate the exact expected graph key.
  defp counterfactual_force_key(query, scopes, hits, profile, expected_key) do
    desc =
      Coverage.describe(
        query,
        scopes,
        [
          candidate_keys: [expected_key],
          profile: profile,
          entity_serve: false,
          semantic_route: {:neighborhood, :network}
        ] ++ neighborhood_opts(query, scopes, [], force: %{network: [expected_key]})
      )

    {decision, audit, ms} = run_gate(desc, scopes, [])

    # A generative judge asked once yields an outcome, not a rate. Repeat the SAME
    # descriptor so run-to-run variance can be separated from a real false negative.
    repeats =
      for _ <- 1..repeat_n(), reduce: [] do
        acc ->
          {_d, a, _ms} = run_gate(desc, scopes, [])
          [inspect(a && a.stage2) | acc]
      end

    %{decision: decision, intent: audit && audit.intent, blockers: (audit && audit.blockers) || [],
      stage2: audit && audit.stage2, ms: ms, descriptor: describe_summary(desc),
      hits_unused: length(hits),
      stage2_repeats: Enum.reverse(repeats),
      stage2_repeat_tally: repeats |> Enum.frequencies()}
  end

  defp repeat_n do
    case Integer.parse(System.get_env("LEARNER_TRACE_REPEAT", "0")) do
      {n, _} when n > 0 -> n
      _ -> 0
    end
  end

  # C. The Stage-2 veto cannot be the cause: an entail_fun that always entails.
  defp counterfactual_bypass_stage2(descriptor, scopes) do
    {decision, audit, ms} = run_gate(descriptor, scopes, entail_fun: fn _q, _g -> true end)
    %{decision: decision, intent: audit && audit.intent, blockers: (audit && audit.blockers) || [],
      stage2: audit && audit.stage2, ms: ms}
  end

  # --- replicating Core's wiring, deliberately not reusing its private functions -------

  defp descriptor(query, scopes, hits, profile, semantic) do
    {route, candidate_opts} =
      case semantic do
        %{route: route, query_vec: vec} -> {route, [query_vec: vec]}
        %{route: route} -> {route, []}
        _ -> {:none, []}
      end

    candidate_keys =
      Enum.uniq(Procedure.candidates(query, scopes, candidate_opts) ++ hit_keys(hits))

    Coverage.describe(
      query,
      scopes,
      [
        candidate_keys: candidate_keys,
        profile: profile,
        entity_serve: false,
        semantic_route: route
      ] ++ neighborhood_opts(query, scopes, candidate_opts, force: %{})
    )
  end

  defp neighborhood_opts(query, scopes, candidate_opts, force: forced) do
    Enum.flat_map(Domain.neighborhood_domains(), fn dom ->
      keys =
        case Map.fetch(forced, dom.key) do
          {:ok, k} -> k
          :error -> Enum.uniq(dom.candidates_fun.(query, scopes, candidate_opts))
        end

      [
        {:"#{dom.key}_keys", keys},
        {:"#{dom.key}_serve", serve?(dom)},
        {:"#{dom.key}_min_corroboration", min_corroboration(dom)}
      ]
    end)
  end

  defp semantic_fallback?(%Coverage.Descriptor{intent: :unknown}), do: true

  defp semantic_fallback?(%Coverage.Descriptor{blockers: blockers}),
    do: Enum.any?(blockers, &(&1 in [:no_candidate, :no_corroboration]))

  defp run_gate(descriptor, scopes, extra) do
    opts = Keyword.merge([scopes: scopes], extra)
    started = System.monotonic_time(:millisecond)

    result =
      try do
        Gate.sufficient?(descriptor, opts)
      rescue
        e -> {:error, e}
      end

    ms = System.monotonic_time(:millisecond) - started

    case result do
      {:serve, _answer, audit} -> {:serve, audit, ms}
      {:escalate, audit} -> {:escalate, audit, ms}
      {:error, e} -> {"raised: #{inspect(e)}", nil, ms}
    end
  end

  # --- fact cuts: where along the read does the inventory fact disappear? --------------

  # What the `hosted_on` edge actually POINTS AT. Counting facts and listing relation
  # names never showed this, so "the correct fact was present" was inherited assumption
  # rather than something the artifact demonstrated.
  defp hosted_on_object(key, scopes) do
    %{rows: rows} =
      Repo.query!(
        """
        SELECT d.key FROM edge e
          JOIN node s ON s.id = e.src
          JOIN node d ON d.id = e.dst
         WHERE s.key = $1 AND e.type = 'hosted_on'
           AND e.visibility_scope = ANY($2) AND d.scope = ANY($2) AND s.scope = ANY($2)
        """,
        [key, scopes]
      )

    Enum.map(rows, fn [k] -> k end)
  end

  # Presence in a candidate list is NOT successful binding. What matters is the key the
  # descriptor actually bound, so record it and compare.
  defp binding_check(%Coverage.Descriptor{} = d, expected_key) do
    %{
      bound_key: d.neighborhood_key,
      expected_key: expected_key,
      bound: d.neighborhood_key != nil,
      matches_expected: d.neighborhood_key == expected_key
    }
  end

  defp hosted_on_check(expected_key, expected_node_key, scopes) do
    objects = hosted_on_object(expected_key, scopes)

    %{
      objects: objects,
      expected_node_key: expected_node_key,
      present: expected_node_key in objects,
      count: length(objects)
    }
  end

  defp fact_cuts(key, scopes) do
    %{rows: [[unfiltered]]} =
      Repo.query!("SELECT count(*) FROM edge e JOIN node s ON s.id = e.src WHERE s.key = $1", [key])

    %{rows: [[after_scope]]} =
      Repo.query!(
        """
        SELECT count(*) FROM edge e
          JOIN node s ON s.id = e.src
          JOIN node d ON d.id = e.dst
         WHERE s.key = $1 AND e.visibility_scope = ANY($2)
           AND d.scope = ANY($2) AND s.scope = ANY($2) AND e.reward >= 0
        """,
        [key, scopes]
      )

    min_corr = min_corroboration(network_domain())

    %{rows: [[after_corr]]} =
      Repo.query!(
        """
        SELECT count(*) FROM edge e
          JOIN node s ON s.id = e.src
          JOIN node d ON d.id = e.dst
         WHERE s.key = $1 AND e.visibility_scope = ANY($2)
           AND d.scope = ANY($2) AND s.scope = ANY($2) AND e.reward >= 0
           AND e.seen_count >= $3
        """,
        [key, scopes, min_corr]
      )

    no_fresh = Network.neighborhood(key, scopes, min_corroboration: min_corr, freshness: false)
    with_fresh = Network.neighborhood(key, scopes, min_corroboration: min_corr, freshness: true)

    %{
      unfiltered: unfiltered,
      after_scope: after_scope,
      after_corroboration: after_corr,
      after_freshness: length(with_fresh),
      reader_without_freshness: length(no_fresh),
      min_corroboration: min_corr,
      relations_after_freshness: with_fresh |> Enum.map(& &1[:relation] || &1[:type]) |> Enum.uniq()
    }
  end

  # --- small helpers -------------------------------------------------------------------

  defp describe_summary(%Coverage.Descriptor{} = d) do
    %{
      intent: d.intent,
      domain: d.domain,
      blockers: d.blockers,
      neighborhood_subject: d.neighborhood_subject,
      neighborhood_key: d.neighborhood_key,
      fact_count: length(d.neighborhood_facts || []),
      validated:
        case Coverage.validate(d) do
          {:ok, _} -> "ok"
          {:error, blockers} -> inspect(blockers)
        end
    }
  end

  defp network_domain, do: Enum.find(Domain.neighborhood_domains(), &(&1.key == :network))

  defp network_candidate_keys(query, scopes, candidate_opts),
    do: Enum.uniq(network_domain().candidates_fun.(query, scopes, candidate_opts))

  defp serve?(dom),
    do: Application.get_env(:swarm, :tier_gate, [])[dom.serve_opt] == true

  defp min_corroboration(dom),
    do:
      Application.get_env(:swarm, :tier_gate, [])[:"#{dom.key}_min_corroboration"] ||
        dom.min_corroboration

  defp breaker_ms,
    do: Application.get_env(:swarm, :tier_gate, [])[:breaker_ms] || 3_000

  defp retrieval_hits(query, scopes) do
    Swarm.Graph.Retrieval.search(query, scopes, limit: 12, expand: false)
    |> Map.fetch!(:memories)
  rescue
    _ -> []
  end

  defp hit_keys(hits), do: hits |> Enum.map(& &1.key) |> Enum.uniq() |> Enum.take(8)

  defp summary(traced, scopes) do
    tally = fn f -> traced |> Enum.map(f) |> Enum.frequencies() end

    conditions = conditions(scopes)

    %{
      kind: "learner_eval_trace_summary",
      traced_at: DateTime.utc_now() |> DateTime.to_iso8601(),
      condition_hash: conditions.condition_hash,
      conditions: conditions,
      rows: length(traced),
      scopes: scopes,
      tier_gate: Application.get_env(:swarm, :tier_gate, []) |> Enum.into(%{}, fn {k, v} -> {k, to_string(v)} end),
      cheap_intent: tally.(& &1.cheap.intent),
      cheap_blockers: tally.(&inspect(&1.cheap.blockers)),
      semantic_route: tally.(&inspect(&1.semantic.route)),
      effective_intent: tally.(& &1.semantic.descriptor.intent),
      effective_blockers: tally.(&inspect(&1.semantic.descriptor.blockers)),
      expected_key_present: tally.(& &1.candidates.expected_present),
      stage2: tally.(&inspect(&1.gate.stage2)),
      decision: tally.(&to_string(&1.gate.decision)),
      counterfactual_force_network: tally.(&to_string(&1.counterfactuals.force_network.decision)),
      counterfactual_force_key: tally.(&to_string(&1.counterfactuals.force_key.decision)),
      counterfactual_bypass_stage2: tally.(&to_string(&1.counterfactuals.bypass_stage2.decision)),
      stage2_repeat_stability:
        traced
        |> Enum.map(fn r ->
          t = r.counterfactuals.force_key.stage2_repeat_tally
          {r.row_id, t}
        end)
        |> Enum.into(%{}),
      stage2_rows_with_mixed_verdicts:
        Enum.count(traced, &(map_size(&1.counterfactuals.force_key.stage2_repeat_tally) > 1)),
      facts_zero_after_freshness: Enum.count(traced, &(&1.facts.after_freshness == 0)),
      facts_zero_after_scope: Enum.count(traced, &(&1.facts.after_scope == 0)),
      hosted_on_object_matches_expected: Enum.count(traced, & &1.hosted_on.present),
      bound_at_all: Enum.count(traced, & &1.binding.bound),
      bound_to_expected_key: Enum.count(traced, & &1.binding.matches_expected),
      expected_rank_zero: Enum.count(traced, &(&1.candidates.expected_rank == 0)),
      slowest_gate_ms: traced |> Enum.map(& &1.gate.ms) |> Enum.max(fn -> 0 end)
    }
  end

  # Every measurement carries its conditions. This artifact did not, and we have demanded
  # it of every other one all week.
  defp conditions(scopes) do
    %{rows: [[nodes, edges, latest]]} =
      Repo.query!(
        "SELECT (SELECT count(*) FROM node), (SELECT count(*) FROM edge), " <>
          "(SELECT max(updated_at)::text FROM edge)",
        []
      )

    base = %{
      set_file: System.get_env("LEARNER_TRACE_SET", ""),
      set_sha256: file_sha256(System.get_env("LEARNER_TRACE_SET", "")),
      database: System.get_env("SWARM_DB_NAME", ""),
      swarm_env: System.get_env("SWARM_ENV", ""),
      ml_address: System.get_env("SWARM_ML_ADDRESS", ""),
      swarm_revision: git(Path.expand("../../swarm", __DIR__), ["rev-parse", "HEAD"], "unknown"),
      swarm_dirty?: git(Path.expand("../../swarm", __DIR__), ["status", "--porcelain"], "") != "",
      hive_revision: git(Path.expand("..", __DIR__), ["rev-parse", "HEAD"], "unknown"),
      hive_dirty?: git(Path.expand("..", __DIR__), ["status", "--porcelain"], "") != "",
      tier_gate: Application.get_env(:swarm, :tier_gate, []) |> Enum.into(%{}, fn {k, v} -> {k, to_string(v)} end),
      scopes: scopes,
      graph_nodes: nodes,
      graph_edges: edges,
      graph_latest_edge: latest
    }

    hash =
      base |> Jason.encode!() |> then(&:crypto.hash(:sha256, &1)) |> Base.encode16(case: :lower)

    Map.put(base, :condition_hash, hash)
  end

  defp file_sha256(""), do: ""

  defp file_sha256(path) do
    case File.read(path) do
      {:ok, bin} -> bin |> then(&:crypto.hash(:sha256, &1)) |> Base.encode16(case: :lower)
      _ -> ""
    end
  end

  defp git(path, args, fallback) do
    case System.cmd("git", ["-C", path | args], stderr_to_stdout: true) do
      {out, 0} -> String.trim(out)
      _ -> fallback
    end
  end

  defp scopes do
    %{rows: rows} =
      Repo.query!(
        """
        WITH s AS (
          SELECT scope FROM node WHERE scope IS NOT NULL AND scope <> ''
          UNION
          SELECT visibility_scope FROM edge WHERE visibility_scope IS NOT NULL AND visibility_scope <> ''
        )
        SELECT scope FROM s ORDER BY scope
        """,
        []
      )

    Enum.map(rows, fn [s] -> s end)
  end

  defp read_jsonl!(path) do
    path
    |> File.stream!()
    |> Stream.map(&String.trim/1)
    |> Stream.reject(&(&1 == ""))
    |> Enum.map(&Jason.decode!/1)
  end

  defp write_jsonl!(path, rows) do
    File.mkdir_p!(Path.dirname(path))
    File.write!(path, Enum.map_join(rows, "\n", &Jason.encode!(jsonable(&1))) <> "\n")
  end

  # Routes are tuples (`{:neighborhood, :network}`) and Jason has no encoder for those.
  # Stringify rather than reshape, so the recorded value still reads as what the code saw.
  defp jsonable(%{} = map) when not is_struct(map),
    do: Map.new(map, fn {k, v} -> {k, jsonable(v)} end)

  defp jsonable(list) when is_list(list), do: Enum.map(list, &jsonable/1)
  defp jsonable(tuple) when is_tuple(tuple), do: inspect(tuple)
  defp jsonable(other), do: other

  defp require_env!(name) do
    case System.get_env(name) do
      v when is_binary(v) and v != "" -> v
      _ -> raise "#{name} is required"
    end
  end
end

LearnerEvalTrace.run()
