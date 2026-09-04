# Learner-eval runner: ask Swarm every question in a frozen (or live) set and record
# what it said. Nothing here grades and nothing here writes to the graph -- the
# grader is the Proxmox API, in a separate deterministic step, so this script has
# no opinion to leak into the number.
#
# Each row may carry a `control_question`: the same ask phrased with the raw
# hostname. Both are asked, so the join question and its single-source control
# are measured under identical conditions on the same subject.
#
# Usage (compose-derived environment, never hand-copied variables):
#
#   export SWARM_ENV=staging
#   eval "$(hive/scripts/kernel-measure-env)"
#   LEARNER_EVAL_FILE=hive/tmp/learner-eval/frozen.jsonl \
#     LEARNER_EVAL_OUT=hive/tmp/learner-eval/frozen_run1.jsonl \
#     MIX_ENV=dev mise exec -C swarm/kernel -- mix run --no-start hive/scripts/learner_eval_run.exs

require Logger

Logger.configure(level: :warning)

defmodule LearnerEvalRun do
  @moduledoc false

  alias Swarm.Core
  alias Swarm.Repo

  def run do
    input = require_env!("LEARNER_EVAL_FILE")
    [meta | rows] = read_jsonl!(input)

    if rows == [], do: raise("#{input} has a header but no question rows")

    {:ok, _} = Application.ensure_all_started(:swarm)

    scopes = scopes()
    conditions = conditions(input, meta, scopes)
    viewer = System.get_env("LEARNER_EVAL_VIEWER", "")

    IO.puts(
      "learner-eval: set=#{Map.get(meta, "label")} rows=#{length(rows)} " <>
        "set_hash=#{Map.get(meta, "set_hash")} condition_hash=#{conditions.condition_hash}"
    )

    results =
      rows
      |> Enum.with_index(1)
      |> Enum.map(fn {row, i} ->
        result = ask_row(row, scopes, viewer)

        IO.puts(
          "  [#{i}/#{length(rows)}] #{row["row_id"]} #{row["shape"]} " <>
            "#{result.swarm_status} #{result.duration_ms}ms" <>
            control_note(result)
        )

        result
      end)

    out = out_path()

    write_jsonl!(out, [
      %{
        kind: "learner_eval_run",
        measured_at: DateTime.utc_now() |> DateTime.to_iso8601(),
        set: meta,
        condition_hash: conditions.condition_hash,
        conditions: conditions,
        rows: length(results)
      }
      | results
    ])

    IO.puts("learner-eval: wrote #{out}")
  end

  defp ask_row(row, scopes, viewer) do
    {answer, duration_ms} = timed_ask(row["question"], scopes, viewer)

    base = %{
      kind: "learner_eval_answer",
      row_id: row["row_id"],
      shape: row["shape"],
      join: row["join"],
      name_identity: row["name_identity"],
      site: row["site"],
      subject: row["subject"],
      question: row["question"],
      expect: row["expect"],
      swarm_status: to_string(answer.status),
      swarm_tier: answer.tier,
      swarm_confidence: answer.confidence,
      swarm_answer: answer.answer,
      citations: Enum.map(answer.citations, &Map.take(&1, [:source, :ref, :url])),
      # What the answer was BUILT FROM, straight from the kernel. Answer text cannot
      # show whether a fact came from the inventory or from a document that happened to
      # mention it, so the grader must not have to guess (the concordance ceiling,
      # docs/design/learner-eval-grading.md). Absent on tier-0, errors and not-found.
      provenance: Map.get(answer, :provenance),
      duration_ms: duration_ms
    }

    case row["control_question"] do
      q when is_binary(q) and q != "" ->
        {control, control_ms} = timed_ask(q, scopes, viewer)

        Map.merge(base, %{
          control_question: q,
          control_status: to_string(control.status),
          control_tier: control.tier,
          control_answer: control.answer,
          control_citations: Enum.map(control.citations, &Map.take(&1, [:source, :ref, :url])),
          control_provenance: Map.get(control, :provenance),
          control_duration_ms: control_ms
        })

      _ ->
        base
    end
  end

  defp timed_ask(question, scopes, viewer) do
    started = System.monotonic_time(:millisecond)
    answer = Core.ask(question, scopes: scopes, viewer: viewer)
    {answer, System.monotonic_time(:millisecond) - started}
  end

  defp control_note(%{control_status: status, control_duration_ms: ms}),
    do: " | control #{status} #{ms}ms"

  defp control_note(_), do: ""

  defp conditions(input, meta, scopes) do
    base = %{
      hive_revision: git_revision(Path.expand("..", __DIR__)),
      swarm_revision: git_revision(Path.expand("../../swarm", __DIR__)),
      hive_dirty?: git_dirty?(Path.expand("..", __DIR__)),
      swarm_dirty?: git_dirty?(Path.expand("../../swarm", __DIR__)),
      swarm_env: System.get_env("SWARM_ENV", ""),
      database: System.get_env("SWARM_DB_NAME", ""),
      ml_address: System.get_env("SWARM_ML_ADDRESS", ""),
      consilium_panel: System.get_env("SWARM_CONSILIUM_PANEL", ""),
      consilium_judge: System.get_env("SWARM_CONSILIUM_JUDGE", ""),
      tier_gate: normalize(Application.get_env(:swarm, :tier_gate, [])),
      scopes: scopes,
      input_sha256: file_sha256(input),
      set_hash: Map.get(meta, "set_hash"),
      truth_observed_at: Map.get(meta, "truth_observed_at"),
      grader: "proxmox_api"
    }

    hash =
      base
      |> Jason.encode!()
      |> then(&:crypto.hash(:sha256, &1))
      |> Base.encode16(case: :lower)

    Map.put(base, :condition_hash, hash)
  end

  defp normalize(value) when is_list(value) do
    if Keyword.keyword?(value) do
      Map.new(value, fn {key, item} -> {to_string(key), normalize(item)} end)
    else
      Enum.map(value, &normalize/1)
    end
  end

  defp normalize(value) when is_map(value),
    do: Map.new(value, fn {key, item} -> {to_string(key), normalize(item)} end)

  defp normalize(value) when is_atom(value), do: to_string(value)
  defp normalize(value), do: value

  defp scopes do
    case System.get_env("QA_SCOPES") do
      nil ->
        %{rows: rows} =
          Ecto.Adapters.SQL.query!(
            Repo,
            """
            WITH scopes AS (
              SELECT scope FROM node WHERE scope IS NOT NULL AND scope <> ''
              UNION
              SELECT visibility_scope FROM edge WHERE visibility_scope IS NOT NULL AND visibility_scope <> ''
            )
            SELECT scope FROM scopes ORDER BY scope
            """,
            []
          )

        Enum.map(rows, fn [scope] -> scope end)

      value ->
        String.split(value, ",", trim: true)
    end
  end

  defp read_jsonl!(path) do
    path
    |> File.stream!()
    |> Stream.map(&String.trim/1)
    |> Stream.reject(&(&1 == "" or String.starts_with?(&1, "#")))
    |> Enum.map(&Jason.decode!/1)
  end

  defp write_jsonl!(path, rows) do
    File.mkdir_p!(Path.dirname(path))
    File.write!(path, Enum.map_join(rows, "\n", &Jason.encode!/1) <> "\n")
  end

  defp out_path do
    System.get_env("LEARNER_EVAL_OUT") ||
      raise "LEARNER_EVAL_OUT is required"
  end

  defp require_env!(name) do
    case System.get_env(name) do
      nil -> raise "#{name} is required"
      "" -> raise "#{name} is required"
      value -> value
    end
  end

  defp file_sha256(path) do
    path |> File.read!() |> then(&:crypto.hash(:sha256, &1)) |> Base.encode16(case: :lower)
  end

  defp git_revision(path), do: git(path, ["rev-parse", "HEAD"], "unknown")
  defp git_dirty?(path), do: git(path, ["status", "--porcelain"], "") != ""

  defp git(path, args, fallback) do
    case System.cmd("git", ["-C", path | args], stderr_to_stdout: true) do
      {out, 0} -> String.trim(out)
      _ -> fallback
    end
  end
end

LearnerEvalRun.run()
