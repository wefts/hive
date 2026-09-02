# GLPI ground-truth eval harness.
#
# Evaluation only: nothing from GLPI is written to the graph. Input and output
# paths should be gitignored because tickets may contain names, emails, customer
# references, and temporal operational details.
#
# Input JSONL schema:
#   {"ticket_id":"12345","question":"...","reference_answer":"...","corpus_answerable":true}
#
# The reference answer should be derived from closing followups by the operator or
# an explicit extraction step. For cloud grading, set GLPI_GRADE_WITH_GEMINI=true;
# this sends question/reference/Swarm answer to Gemini and is therefore limited to
# the operator-approved staging evaluation boundary.
#
# Usage:
#
#   cd swarm/kernel
#   export SWARM_ENV=staging
#   eval "$(cd ../../hive && scripts/kernel-measure-env)"
#   GLPI_EVAL_FILE=../../hive/tmp/glpi-eval/sample.jsonl \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/glpi_ground_truth_eval.exs
#
# Optional cloud grading:
#
#   set -a; . ../../hive/secrets.env; set +a
#   GLPI_GRADE_WITH_GEMINI=true GEMINI_MODEL=gemini-pro-latest \
#     GLPI_EVAL_FILE=../../hive/tmp/glpi-eval/sample.jsonl \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/glpi_ground_truth_eval.exs

require Logger

Logger.configure(level: :warning)

defmodule GlpiGroundTruthEval do
  @moduledoc false

  alias Swarm.Core
  alias Swarm.Repo

  @classes [
    "answered_correctly",
    "answered_wrongly",
    "honestly_escalated",
    "not_answerable_from_corpus"
  ]

  def run do
    input = require_env!("GLPI_EVAL_FILE")
    rows = read_jsonl!(input)

    if rows == [] do
      raise "GLPI_EVAL_FILE contained no rows"
    end

    {:ok, _} = Application.ensure_all_started(:swarm)

    conditions = conditions(input)
    scopes = scopes()
    grade_with_gemini? = env_bool("GLPI_GRADE_WITH_GEMINI")
    gemini_key = if grade_with_gemini?, do: require_env!("GEMINI_API_KEY"), else: nil
    gemini_model = System.get_env("GEMINI_MODEL", "gemini-pro-latest")

    results =
      Enum.map(rows, fn row ->
        evaluate_ticket(row, scopes, grade_with_gemini?, gemini_key, gemini_model)
      end)

    summary = summarize(results, conditions)
    write_jsonl!(out_path(), [Map.put(summary, :kind, "glpi_ground_truth_summary") | results])

    IO.puts("glpi-ground-truth: input=#{input}")

    IO.puts(
      "glpi-ground-truth: count=#{length(results)} condition_hash=#{conditions.condition_hash}"
    )

    for {class, count} <- summary.counts do
      IO.puts("SUMMARY\t#{class}=#{count}")
    end

    IO.puts("glpi-ground-truth: wrote #{out_path()}")
  end

  defp evaluate_ticket(row, scopes, grade_with_gemini?, gemini_key, gemini_model) do
    question = fetch_field!(row, "question")
    started = System.monotonic_time(:millisecond)
    answer = Core.ask(question, scopes: scopes, viewer: System.get_env("GLPI_VIEWER", ""))
    duration_ms = System.monotonic_time(:millisecond) - started

    base = %{
      kind: "glpi_ground_truth_result",
      ticket_id: Map.get(row, "ticket_id") || Map.get(row, "id"),
      question: question,
      reference_answer: fetch_field!(row, "reference_answer"),
      corpus_answerable: Map.get(row, "corpus_answerable"),
      swarm_status: to_string(answer.status),
      swarm_tier: answer.tier,
      swarm_confidence: answer.confidence,
      swarm_answer: answer.answer,
      duration_ms: duration_ms,
      citations: Enum.map(answer.citations, &Map.take(&1, [:source, :ref, :url]))
    }

    grade =
      if grade_with_gemini? do
        gemini_grade(gemini_key, gemini_model, base)
      else
        deterministic_grade(base)
      end

    Map.merge(base, grade)
  end

  defp deterministic_grade(%{swarm_status: status, corpus_answerable: false})
       when status != "found" do
    %{classification: "not_answerable_from_corpus", grader: "deterministic", grade_status: "ok"}
  end

  defp deterministic_grade(%{swarm_status: status}) when status != "found" do
    %{classification: "honestly_escalated", grader: "deterministic", grade_status: "ok"}
  end

  defp deterministic_grade(_row) do
    %{
      classification: "needs_review",
      grader: "deterministic",
      grade_status: "needs_review",
      rationale: "found answers require reference-answer judgement"
    }
  end

  defp gemini_grade(key, model, row) do
    prompt = """
    You are grading a staging-only GLPI ground-truth evaluation. The ticket text may contain operational details.

    Choose exactly one class:
    - answered_correctly: Swarm answers the question and agrees with the reference answer.
    - answered_wrongly: Swarm answers but contradicts, fabricates, or materially misses the reference answer.
    - honestly_escalated: Swarm does not answer and the reference indicates the corpus should contain the answer.
    - not_answerable_from_corpus: the reference indicates this fact is not expected to be in the current corpus.

    QUESTION:
    #{row.question}

    REFERENCE ANSWER:
    #{row.reference_answer}

    CORPUS_ANSWERABLE:
    #{inspect(row.corpus_answerable)}

    SWARM STATUS:
    #{row.swarm_status}

    SWARM ANSWER:
    #{row.swarm_answer}

    Respond as JSON only:
    {"classification": string, "score": number between 0 and 1, "rationale": string}
    """

    body =
      Jason.encode!(%{
        contents: [%{role: "user", parts: [%{text: prompt}]}],
        generationConfig: %{temperature: 0, responseMimeType: "application/json"}
      })

    url = ~c"https://generativelanguage.googleapis.com/v1beta/models/#{model}:generateContent"

    headers = [
      {~c"content-type", ~c"application/json"},
      {~c"x-goog-api-key", String.to_charlist(key)}
    ]

    request = {url, headers, ~c"application/json", body}

    case :httpc.request(:post, request, [{:timeout, 120_000}], body_format: :binary) do
      {:ok, {{_, status, _}, _headers, resp}} when status in 200..299 ->
        resp
        |> extract_gemini_text()
        |> parse_grade()

      {:ok, {{_, status, _}, _headers, resp}} ->
        %{
          classification: "needs_review",
          grader: model,
          grade_status: "error",
          grade_error: "http_#{status}",
          grade_body: truncate(resp)
        }

      {:error, reason} ->
        %{
          classification: "needs_review",
          grader: model,
          grade_status: "error",
          grade_error: inspect(reason)
        }
    end
  end

  defp parse_grade(text) do
    text
    |> extract_json_object()
    |> Jason.decode()
    |> case do
      {:ok, %{"classification" => class} = parsed} when class in @classes ->
        %{
          classification: class,
          score: Map.get(parsed, "score", 0.0) / 1,
          rationale: Map.get(parsed, "rationale", ""),
          grader: System.get_env("GEMINI_MODEL", "gemini-pro-latest"),
          grade_status: "ok"
        }

      {:ok, _} ->
        %{
          classification: "needs_review",
          grader: System.get_env("GEMINI_MODEL", "gemini-pro-latest"),
          grade_status: "error",
          grade_error: "invalid_grade_schema",
          grade_body: truncate(text)
        }

      {:error, reason} ->
        %{
          classification: "needs_review",
          grader: System.get_env("GEMINI_MODEL", "gemini-pro-latest"),
          grade_status: "error",
          grade_error: inspect(reason),
          grade_body: truncate(text)
        }
    end
  end

  defp summarize(results, conditions) do
    counts =
      results
      |> Enum.map(& &1.classification)
      |> Enum.frequencies()
      |> Map.new(fn {class, count} -> {class, count} end)

    %{
      measured_at: DateTime.utc_now() |> DateTime.to_iso8601(),
      condition_hash: conditions.condition_hash,
      conditions: conditions,
      counts: counts,
      corpus_gaps: Map.get(counts, "not_answerable_from_corpus", 0),
      retrieval_or_synthesis_failures: Map.get(counts, "answered_wrongly", 0),
      honest_escalations: Map.get(counts, "honestly_escalated", 0)
    }
  end

  defp conditions(input) do
    base = %{
      hive_revision: git_revision(Path.expand("..", __DIR__)),
      swarm_revision: git_revision(Path.expand("../../swarm", __DIR__)),
      hive_dirty?: git_dirty?(Path.expand("..", __DIR__)),
      swarm_dirty?: git_dirty?(Path.expand("../../swarm", __DIR__)),
      swarm_env: System.get_env("SWARM_ENV", ""),
      database: System.get_env("SWARM_DB_NAME", ""),
      ml_address: System.get_env("SWARM_ML_ADDRESS", ""),
      input_sha256: file_sha256(input),
      grader:
        if(env_bool("GLPI_GRADE_WITH_GEMINI"),
          do: System.get_env("GEMINI_MODEL", "gemini-pro-latest"),
          else: "deterministic"
        )
    }

    hash =
      base
      |> Jason.encode!()
      |> then(&:crypto.hash(:sha256, &1))
      |> Base.encode16(case: :lower)

    Map.put(base, :condition_hash, hash)
  end

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

  defp extract_gemini_text(resp) do
    case Jason.decode(resp) do
      {:ok, %{"candidates" => [%{"content" => %{"parts" => parts}} | _]}} ->
        Enum.map_join(parts, "", &Map.get(&1, "text", ""))

      {:ok, other} ->
        Jason.encode!(other)

      {:error, _} ->
        resp
    end
  end

  defp extract_json_object(text) do
    case Regex.run(~r/\{.*\}/s, text) do
      [json] -> json
      nil -> text
    end
  end

  defp write_jsonl!(path, rows) do
    File.mkdir_p!(Path.dirname(path))
    File.write!(path, Enum.map_join(rows, "\n", &Jason.encode!/1) <> "\n")
  end

  defp out_path do
    System.get_env("GLPI_EVAL_OUT") ||
      Path.expand("../tmp/glpi-eval/latest.jsonl", __DIR__)
  end

  defp fetch_field!(row, field) do
    case Map.get(row, field) do
      value when is_binary(value) and value != "" ->
        value

      _ ->
        raise "GLPI eval row #{inspect(Map.get(row, "ticket_id") || Map.get(row, "id"))} missing #{field}"
    end
  end

  defp require_env!(name) do
    case System.get_env(name) do
      nil -> raise "#{name} is required"
      "" -> raise "#{name} is required"
      value -> value
    end
  end

  defp env_bool(name), do: System.get_env(name) in ["1", "true", "yes"]

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

  defp truncate(text) when is_binary(text), do: String.slice(text, 0, 500)
end

:inets.start()
:ssl.start()
GlpiGroundTruthEval.run()
