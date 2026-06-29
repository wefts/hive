# Integrated cognitive-loop harness (CTC-1) — the FIRST time enrichment + entity-
# resolution + origin accounting + relaxation run together, on a PERSISTENT shadow.
#
# Council-shaped: the loop MUTATES state, so it is STAGED + REVERSIBLE (the shadow is
# the staging layer; promotion to prod is the operator's reviewed go/no-go — never
# here). Cadence-separated: enrich K rounds → measure → ER BATCH (lagging, stricter)
# → measure. A CIRCUIT-BREAKER halts + rolls back on emergent feedback poisoning,
# with TOP-K CONCENTRATION as the primary signal (codex). Aggregate metrics only —
# never prints content.
#
#   MODE=shakedown|runaway|real CYCLES=2 SWARM_DB_NAME=swarm_shadow \
#     SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev \
#     mise exec -- mix run --no-start ../../hive/scripts/cognitive_loop.exs
#
# MODE: shakedown = fast deterministic mocks (loop should converge, no breaker);
#       runaway   = mocks that funnel every claim into one super-node (breaker MUST fire + roll back);
#       real      = the live local model (operator; ~120 s/source).

require Logger
Logger.configure(level: :warning)
alias Swarm.Enrichment.Scheduler
alias Swarm.EntityResolution.Resolver
alias Swarm.Repo

db = System.get_env("SWARM_DB_NAME", "swarm_shadow")
if db == "swarm_dev", do: raise("REFUSED: cognitive_loop must never run on swarm_dev (conditional-prod)")
# NB: this env-var view is NOT the connected DB. In MIX_ENV=dev, runtime.exs defaults
# the Repo to swarm_dev when SWARM_DB_NAME is unset — so this `db` (default swarm_shadow)
# could disagree with where the Repo actually connects. The real guard is below, after
# Repo.start_link: assert current_database() so the script can never silently mutate
# conditional-prod nor diverge from its own gauges (CTC-5 finding #1).

# LOOP_MODE (not MODE — that collides with an ambient MODE=cli in this shell).
mode = System.get_env("LOOP_MODE", "shakedown")
cycles = String.to_integer(System.get_env("CYCLES", "2"))
enrich_rounds = String.to_integer(System.get_env("ENRICH_ROUNDS", "2"))

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)
# `real` mode's enrichment generates over the ML boundary, which checks out a
# long-lived channel from Swarm.ML.ChannelPool (the boundary-crash fix). Under
# `mix run --no-start` the app supervisor is absent, so start the pool explicitly —
# without it every enrich no-ops with a generation_failed error watermark (mirrors
# hive/scripts/ingest_prod.exs).
{:ok, _} = Swarm.ML.ChannelPool.start_link([])

# Truthful DB guard (CTC-5 finding #1): check the database the Repo ACTUALLY connected
# to, not the env-var view above. Refuses conditional-prod, and refuses a silent
# env/runtime-default divergence (where the script's gauges would describe a different
# DB than it mutates). Set SWARM_DB_NAME explicitly — there is no safe default in dev.
%{rows: [[actual_db]]} = Repo.query!("SELECT current_database()")

if actual_db == "swarm_dev" do
  raise("REFUSED: Repo connected to swarm_dev (conditional-prod) — cognitive_loop must never mutate it")
end

if actual_db != db do
  raise(
    "REFUSED: SWARM_DB_NAME=#{db} but the Repo connected to #{actual_db} " <>
      "(env/runtime-default mismatch) — set SWARM_DB_NAME explicitly so gauges and mutations agree"
  )
end

# Bound the loop small + ER stricter than prod (first integrated run, council).
# MAX_PER_PASS is the per-pass enrichment budget — env-tunable so the dry-run can
# scale exposure (1 source for a smoke, a handful for the bounded run) and the
# operator can size the hot run. Default 3 (the council's small first-run bound).
max_per_pass = String.to_integer(System.get_env("MAX_PER_PASS", "3"))
enr = Application.get_env(:swarm, :enrichment, [])
Application.put_env(:swarm, :enrichment, Keyword.merge(enr, max_per_pass: max_per_pass, claim_reliability: 0.5))

defmodule Loop do
  alias Swarm.Repo

  # --- aggregate gauges (no content) ----------------------------------------
  def gauges do
    one = fn sql -> %{rows: [[v]]} = Repo.query!(sql); v end

    entities = one.("SELECT count(*) FROM node WHERE type='entity'")
    claims = one.("SELECT count(*) FROM edge WHERE evidence_kind='claim'")
    merges = one.("SELECT count(*) FROM entity_resolution_audit WHERE decision='confirmed_merged'")
    seen_max = one.("SELECT COALESCE(max(seen_count),0) FROM edge WHERE evidence_kind='claim'")

    # top-1 concentration: the share of claim-edges captured by the SINGLE most-
    # targeted entity (a super-node). Robust at small N (unlike top-10, which
    # trivially → 1.0 when entities < 10); rising sharply ⇒ super-node capture.
    top1 =
      one.("""
      SELECT COALESCE(
        (SELECT max(c) FROM (SELECT count(*) c FROM edge WHERE evidence_kind='claim'
            GROUP BY dst) t)::float
        / NULLIF((SELECT count(*) FROM edge WHERE evidence_kind='claim'),0), 0.0)
      """)

    # case-folded entity-key collision groups still un-merged (fragmentation).
    frag =
      one.("""
      SELECT count(*) FROM (
        SELECT lower(key) lk FROM node WHERE type='entity'
        GROUP BY lower(key) HAVING count(*) > 1) g
      """)

    # ER proposal quality — confirmed:rejected ratio (leading risk indicator: a
    # rising confirm rate before the physical breakers trip; council/gemma).
    rej = one.("SELECT count(*) FROM entity_resolution_audit WHERE decision='rejected'")

    %{entities: entities, claims: claims, merges: merges, seen_max: seen_max,
      top1: Float.round(top1 * 1.0, 3), frag: frag, rejected: rej}
  end

  # Negative control (codex): the loop's read/measure path must not mutate. Measure,
  # do a read-only traversal, measure again — the graph counters must be identical.
  def control_ok? do
    g1 = gauges()
    _ = Swarm.Graph.Traverse.traverse(1, 3)
    g2 = gauges()
    Map.take(g1, [:entities, :claims, :merges]) == Map.take(g2, [:entities, :claims, :merges])
  end

  # --- circuit-breaker (council) --------------------------------------------
  # Compares this round to the previous; returns nil (ok) or a {reason} to halt.
  # The top-1 super-node signal is gated on a minimum claim count — concentration
  # is not meaningful until there are enough edges to share (the small-N artifact
  # the first shakedown caught).
  @min_claims 8
  def breaker(prev, cur) do
    cond do
      cur.claims >= @min_claims and cur.top1 - prev.top1 > 0.3 and cur.top1 > 0.5 ->
        {:concentration_spike, prev.top1, cur.top1}

      prev.entities > 0 and cur.entities < prev.entities * 0.7 ->
        {:entity_collapse, prev.entities, cur.entities}

      prev.entities > 0 and cur.merges - prev.merges > prev.entities * 0.5 ->
        {:merge_rate_spike, cur.merges - prev.merges}

      cur.seen_max > 20 ->
        {:seen_runaway, cur.seen_max}

      true ->
        nil
    end
  end

  # --- rollback (reversibility — the shadow stays the staging seed) ----------
  def rollback do
    Repo.query!("DELETE FROM node WHERE type='entity'")
    Repo.query!("DELETE FROM edge WHERE evidence_kind='claim'")
    Repo.query!("DELETE FROM enrichment_watermark")
    Repo.query!("DELETE FROM entity_resolution_audit")
    :ok
  end

  def fmt(g),
    do: "entities=#{g.entities} claims=#{g.claims} merges=#{g.merges} seen_max=#{g.seen_max} topK1=#{g.top1} frag=#{g.frag} rej=#{g.rejected}"
end

# --- injectable model functions per MODE -------------------------------------
defmodule Inject do
  # deterministic mock: 2 claims/node keyed by the passage hash → distinct entities.
  def shakedown_gen do
    fn _model, prompt, _opts ->
      tag = prompt |> :erlang.phash2() |> Integer.to_string()
      {:ok, ~s({"claims":[{"s":"Entity_#{tag}_A","p":"relates_to","o":"Entity_#{tag}_B"},) <>
        ~s({"s":"Entity_#{tag}_A","p":"is_a","o":"Concept_#{rem(:erlang.phash2(prompt), 5)}"}]})}
    end
  end

  # runaway: EVERY node funnels both claims into ONE shared super-node → top-K spike.
  def runaway_gen do
    fn _model, prompt, _opts ->
      tag = prompt |> :erlang.phash2() |> Integer.to_string()
      {:ok, ~s({"claims":[{"s":"Source_#{tag}","p":"points_to","o":"MEGA_NODE"},) <>
        ~s({"s":"Other_#{tag}","p":"points_to","o":"MEGA_NODE"}]})}
    end
  end

  # mock embed: a low-dim-ish deterministic vector from the key so near-identical
  # keys land near each other (gives ER something to consider). Dim from config.
  def mock_embed do
    dim = Swarm.Config.embedding_dim()
    fn texts ->
      vecs = Enum.map(texts, fn t ->
        base = rem(:erlang.phash2(String.downcase(t)), 97) / 97.0
        [base | List.duplicate(0.0, dim - 1)]
      end)
      {:ok, %{vectors: vecs, namespace: "shadow-mock", dim: dim}}
    end
  end

  # conservative mock confirm: merge only keys equal after case/space normalization.
  def mock_confirm do
    fn pair ->
      norm = fn s -> s |> String.downcase() |> String.replace(~r/\s+/, "") end
      norm.(pair.a_key) == norm.(pair.b_key)
    end
  end
end

gen = if mode == "runaway", do: Inject.runaway_gen(), else: Inject.shakedown_gen()
{enrich_opts, er_opts} =
  case mode do
    "real" -> {[], []}
    _ -> {[gen_fun: gen], [embed_fun: Inject.mock_embed(), confirm_fun: Inject.mock_confirm(),
                           vec_threshold: 0.999, lex_threshold: 0.5]}
  end

IO.puts("== cognitive-loop harness == db=#{db} mode=#{mode} cycles=#{cycles} enrich_rounds=#{enrich_rounds} max_per_pass=#{max_per_pass}")

# Negative control: prove the measure/read path is non-mutating before any hot run.
if mode == "control" do
  ok = Loop.control_ok?()
  IO.puts("control (null-run, measure+read only): graph counters stable = #{ok}")
  IO.puts(if(ok, do: "RESULT: harness is read-only — safe to instrument a hot run.",
              else: "RESULT: DRIFT — measurement mutates state; do NOT run hot."))
  System.halt(if(ok, do: 0, else: 1))
end

base = Loop.gauges()
IO.puts("baseline: #{Loop.fmt(base)}")

# --- the cadence-separated integrated loop -----------------------------------
result =
  Enum.reduce_while(1..cycles, {base, :ok}, fn cycle, {prev, _} ->
    # enrich K rounds (evidence first)
    for r <- 1..enrich_rounds do
      s = Scheduler.run_pass(enrich_opts)
      IO.puts("  cyc#{cycle}.enrich#{r}: #{inspect(Map.take(s, [:considered, :enriched, :skipped_locked]))}")
    end

    mid = Loop.gauges()
    IO.puts("  cyc#{cycle} post-enrich: #{Loop.fmt(mid)}")

    case Loop.breaker(prev, mid) do
      nil ->
        # ER batch (lagging, stricter) — the high-blast-radius mutation
        er = Resolver.run_pass(er_opts)
        IO.puts("  cyc#{cycle}.ER: #{inspect(er)}")
        post = Loop.gauges()
        IO.puts("  cyc#{cycle} post-ER: #{Loop.fmt(post)}")

        case Loop.breaker(mid, post) do
          nil -> {:cont, {post, :ok}}
          reason -> {:halt, {post, {:tripped, reason}}}
        end

      reason ->
        {:halt, {mid, {:tripped, reason}}}
    end
  end)

{final, status} = result
IO.puts("\nfinal: #{Loop.fmt(final)}")

case status do
  :ok ->
    IO.puts("RESULT: loop ran to #{cycles} cycles, no breaker — stable on the shadow.")

  {:tripped, reason} ->
    IO.puts("BREAKER TRIPPED: #{inspect(reason)} — rolling back this run (staged, reversible).")
    Loop.rollback()
    after_rb = Loop.gauges()
    IO.puts("after rollback: #{Loop.fmt(after_rb)}")
    IO.puts("RESULT: poisoning detected + rolled back (breaker works; no promotion to prod).")
end
