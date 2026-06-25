# Calibration analyzer (CTC-2) — turn REAL logged decisions into ADR-8 threshold
# suggestions. READ-ONLY; non-sensitive features only (ids/scores, never content).
#
#   SWARM_DB_NAME=swarm_shadow MIX_ENV=dev \
#     mise exec -- mix run --no-start ../../hive/scripts/calibrate.exs
#
# Reads: entity_resolution_audit (ER vec/lex gate) + enrichment_pass /
# enrichment_decision (reward-gate threshold). Calibrate on the OPERATOR's hot-run
# logs — not synthetic data. The output is a suggestion table, applied to config by
# the operator.
#
# LIMITATION (council, codex + gemma converged): the ER audit only holds pairs that
# already passed the live gate, so this calibrates the threshold UPWARD only and its
# recall is CONDITIONAL on the admitted set — it cannot estimate merges missed BELOW
# the gate. TRUE two-sided calibration needs randomized below-gate shadow sampling: a
# stratified audit set spanning below/near/above the threshold. That is the
# operator's stratified-audit step in the hot run (see board ctc-2b), not this
# read-only analyzer. The enrichment worth_it fraction is a SELECTIVITY sanity-check,
# not a value measure (downstream value = CTC-3 answerability-lift).

require Logger
Logger.configure(level: :warning)
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Repo.start_link()

rows = fn sql -> Repo.query!(sql).rows end
pad = fn n -> n |> Integer.to_string() |> String.pad_leading(6) end
pct = fn x -> "#{:erlang.float_to_binary(x * 1.0, decimals: 3)}" end
r3 = fn x -> :erlang.float_to_binary((x || 0.0) * 1.0, decimals: 3) end

IO.puts("== calibration analyzer (read-only; from real logged decisions) ==\n")

# --- ER soft-match: vec/lex gate -------------------------------------------
# NB: the audit only contains pairs that already PASSED the current gate (the gate
# filters before the LLM confirm), so this calibrates the threshold UPWARD —
# "within the admitted set, where do rejects cluster?" — never downward (lowering
# the gate needs a re-run). confirmed_merged = LLM-positive; rejected = LLM-negative.
IO.puts("# Entity-resolution gate (entity_resolution_audit)")

case rows.("SELECT cosine, lex, decision FROM entity_resolution_audit") do
  [] ->
    IO.puts("  (no ER decisions logged yet — run the loop on a real corpus first)\n")

  audit ->
    conf = Enum.filter(audit, fn [_, _, d] -> d == "confirmed_merged" end)
    rej = Enum.filter(audit, fn [_, _, d] -> d == "rejected" end)
    IO.puts("  decisions: #{length(audit)} (#{length(conf)} merged, #{length(rej)} rejected)")
    total_conf = max(length(conf), 1)

    IO.puts("  cosine sweep — admit pairs with cosine ≥ t; precision = merged/(merged+rejected):")
    IO.puts("    NB: recall* is CONDITIONAL — fraction of LLM-confirmed merges retained among")
    IO.puts("    ALREADY-ADMITTED pairs, NOT true system recall (below-gate merges are unobserved).")
    IO.puts("    t     | merged | rejected | precision | recall*")

    for t <- [0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.99] do
      cm = Enum.count(conf, fn [c, _, _] -> c >= t end)
      cr = Enum.count(rej, fn [c, _, _] -> c >= t end)
      prec = if cm + cr > 0, do: cm / (cm + cr), else: 0.0
      rec = cm / total_conf

      IO.puts(
        "    #{:erlang.float_to_binary(t, decimals: 2)}  | #{pad.(cm)} | #{pad.(cr)} | " <>
          "#{pct.(prec)} | #{pct.(rec)}"
      )
    end

    # suggest the highest t holding recall ≥ 0.9 (cut wasted LLM confirms cheaply).
    suggestion =
      [0.80, 0.85, 0.90, 0.93, 0.95, 0.97, 0.99]
      |> Enum.filter(fn t -> Enum.count(conf, fn [c, _, _] -> c >= t end) / total_conf >= 0.9 end)
      |> List.last()

    IO.puts("  → suggested vec_threshold (≥0.9 recall): #{inspect(suggestion)} (UPWARD only — the gate truncates below)\n")
end

# --- Enrichment reward-gate: priority threshold ----------------------------
IO.puts("# Enrichment reward-gate (enrichment_pass + enrichment_decision)")

case rows.("SELECT candidate_count, worth_it_count, score_min, score_p50, score_p90, score_p99, score_max, threshold FROM enrichment_pass ORDER BY id") do
  [] ->
    IO.puts("  (no enrichment passes logged yet — run the loop first)\n")

  passes ->
    [tc, tw] =
      Enum.reduce(passes, [0, 0], fn [c, w, _, _, _, _, _, _], [ac, aw] -> [ac + c, aw + w] end)

    [_, _, _, p50, p90, p99, mx, thr] = List.last(passes)
    worth_frac = if tc > 0, do: tw / tc, else: 0.0

    IO.puts("  passes: #{length(passes)}; candidates=#{tc}, worth-it=#{tw} (#{pct.(worth_frac)})")
    IO.puts("  latest score dist: p50=#{r3.(p50)} p90=#{r3.(p90)} p99=#{r3.(p99)} max=#{r3.(mx)}; threshold=#{r3.(thr)}")

    # SELECTIVITY sanity-check only — worth_it fraction says whether the gate admits a
    # sane share, NOT whether enrichment is worth its cost (that is downstream VALUE,
    # measured by CTC-3 answerability-lift, not here). A healthy gate sits near p50
    # (~50% worth-it); both LOOSE and TIGHT corrections target p50, not the tail.
    note =
      cond do
        worth_frac > 0.8 -> "gate LOOSE (>80% worth-it) — RAISE threshold toward p50=#{r3.(p50)} (more selective)"
        worth_frac < 0.1 -> "gate TIGHT (<10% worth-it) — LOWER threshold toward p50=#{r3.(p50)} (admit more)"
        true -> "gate selectivity reasonable (#{pct.(worth_frac)} worth-it)"
      end

    IO.puts("  → #{note} [selectivity only — value = CTC-3 answerability-lift]")

    case rows.("SELECT decision, count(*) FROM enrichment_decision GROUP BY decision") do
      [] -> IO.puts("  (no acted-on decisions yet)\n")
      d -> IO.puts("  acted-on: #{inspect(d)}\n")
    end
end

IO.puts("(suggestions are UPWARD-biased by the live gate; the operator applies to config / ADR-8.)")
