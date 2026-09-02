# Synthetic contrastive-comprehension canary, graded by Gemini.
#
# Runs local Swarm consilium combinations against invented contrastive fixtures
# and asks Gemini to grade whether the local answer preserved the direction of
# the contrast. Fixtures are synthetic by construction: no corpus grounding or
# runtime citations are sent to the cloud.
#
# Usage:
#
#   cd swarm/kernel
#   eval "$(cd ../../hive && SWARM_ENV=staging scripts/kernel-measure-env)"
#   set -a; . ../../hive/secrets.env; set +a
#   CONTRAST_COMBOS='qwen3:14b+lfm2.5:8b=>gemma4:31b' \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/synthetic_contrastive_canary.exs
#
# Optional:
#   GEMINI_MODEL=gemini-pro-latest
#   CANARY_OUT=../../hive/tmp/contrastive-canary/run.jsonl
#   CANARY_TREND=../../hive/tmp/contrastive-canary/trend.jsonl

require Logger

Logger.configure(level: :warning)

defmodule SyntheticContrastiveCanary do
  @moduledoc false

  alias Swarm.Consilium
  alias Swarm.ML.ChannelPool

  @allowlisted_models MapSet.new([
                        "gemma4:31b",
                        "qwen3:14b",
                        "lfm2.5:8b",
                        "bge-m3",
                        "granite4.1-guardian:8b"
                      ])

  @fixtures [
    %{
      id: "differs-cache-positive-second",
      sentence:
        "The Orion runner does not provide shared cache storage. This differs from the Borealis runner, which provides S3-compatible shared cache storage.",
      question: "Which runner provides S3-compatible shared cache storage?",
      expected:
        "Borealis runner provides S3-compatible shared cache storage; Orion runner does not."
    },
    %{
      id: "differs-recommendation-second",
      sentence:
        "The Vesta runner is reserved for image builds. This differs from the Helios runner, which is the recommended environment for long-lived CI caching.",
      question: "Which runner is recommended for long-lived CI caching?",
      expected:
        "Helios runner is recommended for long-lived CI caching; Vesta runner is reserved for image builds."
    },
    %{
      id: "unlike-capability-first",
      sentence:
        "Unlike the Juniper gateway, the Meridian gateway supports route health checks for private subnets.",
      question: "Which gateway supports route health checks for private subnets?",
      expected:
        "Meridian gateway supports route health checks; Juniper gateway does not in this sentence."
    },
    %{
      id: "unlike-limitation-second",
      sentence:
        "Unlike the Apollo worker, the Hermes worker cannot mount encrypted scratch volumes.",
      question: "Which worker cannot mount encrypted scratch volumes?",
      expected:
        "Hermes worker cannot mount encrypted scratch volumes; Apollo worker is the contrast."
    },
    %{
      id: "whereas-positive-second",
      sentence:
        "The North bridge rotates credentials manually, whereas the South bridge rotates credentials automatically every hour.",
      question: "Which bridge rotates credentials automatically every hour?",
      expected:
        "South bridge rotates credentials automatically every hour; North bridge rotates them manually."
    },
    %{
      id: "whereas-negative-first",
      sentence:
        "The Amber service lacks tenant isolation, whereas the Cobalt service enforces tenant isolation for every request.",
      question: "Which service lacks tenant isolation?",
      expected: "Amber service lacks tenant isolation; Cobalt service enforces it."
    },
    %{
      id: "rather-than-recommendation",
      sentence:
        "Jobs that require repeatable dependency caches should use the Cypress pool rather than the Delta pool, because only Cypress has a shared cache.",
      question: "Which pool should jobs use for repeatable dependency caches?",
      expected: "They should use the Cypress pool; Delta is the contrast."
    },
    %{
      id: "not-a-but-b",
      sentence:
        "For signed artifact storage, use Harbor Box, not Pebble Box; Pebble Box keeps only temporary build output.",
      question: "Which storage should be used for signed artifact storage?",
      expected:
        "Harbor Box should be used for signed artifact storage; Pebble Box is not the right storage."
    },
    %{
      id: "exception-default",
      sentence:
        "Most Bluefin nodes use the internal resolver, except the Bluefin audit node, which must use the external resolver.",
      question: "Which Bluefin node must use the external resolver?",
      expected: "The Bluefin audit node must use the external resolver."
    },
    %{
      id: "only-second",
      sentence:
        "The Iris profile and Lotus profile both support backups, but only the Lotus profile supports point-in-time restore.",
      question: "Which profile supports point-in-time restore?",
      expected: "Only the Lotus profile supports point-in-time restore."
    },
    %{
      id: "instead-of",
      sentence:
        "For emergency DNS changes, operators should update the Quasar zone instead of the Pulsar zone.",
      question: "Which zone should operators update for emergency DNS changes?",
      expected: "Operators should update the Quasar zone, not the Pulsar zone."
    },
    %{
      id: "negative-exception",
      sentence:
        "All Atlas queues retry failed jobs, except the Atlas import queue, which drops malformed jobs immediately.",
      question: "Which Atlas queue drops malformed jobs immediately?",
      expected: "The Atlas import queue drops malformed jobs immediately."
    }
  ]

  def run do
    key = require_env!("GEMINI_API_KEY")
    model = System.get_env("GEMINI_MODEL", "gemini-pro-latest")
    combos = combos()
    enforce_model_policy!(combos, @allowlisted_models)
    start_swarm!()
    await_ml_pool!()

    conditions = conditions(model, combos)
    rows = evaluate(@fixtures, combos, key, model)
    summary = summarize(rows, conditions)

    write_jsonl!(out_path(), rows)
    append_jsonl!(trend_path(), [summary])

    IO.puts(
      "synthetic-contrastive-canary: fixtures=#{length(@fixtures)} combos=#{length(combos)}"
    )

    IO.puts("synthetic-contrastive-canary: gemini_model=#{model}")
    IO.puts("synthetic-contrastive-canary: condition_hash=#{conditions.condition_hash}")

    for combo <- summary.combos do
      IO.puts(
        "SUMMARY\tcombo=#{combo.combo}\tpreserved=#{combo.preserved}/#{combo.n}\trate=#{Float.round(combo.rate, 3)}\tlocal_errors=#{combo.local_errors}\tgrader_errors=#{combo.grader_errors}"
      )
    end

    IO.puts("synthetic-contrastive-canary: wrote #{out_path()}")
    IO.puts("synthetic-contrastive-canary: trend #{trend_path()}")
  end

  defp evaluate(fixtures, combos, key, gemini_model) do
    for fixture <- fixtures,
        combo <- combos do
      started = System.monotonic_time(:millisecond)

      local =
        Consilium.deliberate(fixture.question,
          grounding: fixture.sentence,
          fleet: %{panel: combo.panel, judge: combo.judge, token_ceiling: 8_000}
        )

      duration_ms = System.monotonic_time(:millisecond) - started

      base = %{
        kind: "synthetic_contrastive_canary",
        fixture_id: fixture.id,
        question: fixture.question,
        sentence: fixture.sentence,
        expected: fixture.expected,
        combo: combo_label(combo),
        panel: combo.panel,
        judge: combo.judge,
        duration_ms: duration_ms
      }

      case local do
        {:ok, verdict} ->
          grade = gemini_grade(key, gemini_model, fixture, verdict.answer)

          base
          |> Map.merge(%{
            local_status: "ok",
            local_answer: verdict.answer,
            local_supported: verdict.supported,
            local_confidence: verdict.confidence
          })
          |> Map.merge(grade)

        {:error, reason} ->
          Map.merge(base, %{
            local_status: "error",
            local_error: inspect(reason),
            gemini_status: "skipped",
            preserved: false,
            score: 0.0,
            rationale: "local consilium did not produce a judged answer"
          })
      end
    end
  end

  defp gemini_grade(key, model, fixture, local_answer) do
    prompt = """
    You are grading a synthetic contrastive-comprehension canary.

    Decide whether the LOCAL ANSWER preserves the direction of the contrast in the SOURCE SENTENCE.
    It is correct only if it assigns the capability, limitation, recommendation, or exception to the same side as EXPECTED.

    SOURCE SENTENCE:
    #{fixture.sentence}

    QUESTION:
    #{fixture.question}

    EXPECTED DIRECTION:
    #{fixture.expected}

    LOCAL ANSWER:
    #{local_answer}

    Respond as JSON only:
    {"preserved": boolean, "score": number between 0 and 1, "rationale": string}
    """

    body =
      Jason.encode!(%{
        contents: [%{role: "user", parts: [%{text: prompt}]}],
        generationConfig: %{
          temperature: 0,
          responseMimeType: "application/json"
        }
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
          gemini_status: "error",
          gemini_error: "http_#{status}",
          gemini_body: truncate(resp),
          preserved: false,
          score: 0.0
        }

      {:error, reason} ->
        %{gemini_status: "error", gemini_error: inspect(reason), preserved: false, score: 0.0}
    end
  end

  defp extract_gemini_text(resp) do
    case Jason.decode(resp) do
      {:ok, %{"candidates" => [%{"content" => %{"parts" => parts}} | _]}} ->
        parts
        |> Enum.map(&Map.get(&1, "text", ""))
        |> Enum.join("")

      {:ok, other} ->
        Jason.encode!(other)

      {:error, _} ->
        resp
    end
  end

  defp parse_grade(text) do
    text
    |> extract_json_object()
    |> Jason.decode()
    |> case do
      {:ok, %{"preserved" => preserved, "score" => score} = parsed}
      when is_boolean(preserved) and is_number(score) ->
        %{
          gemini_status: "ok",
          preserved: preserved,
          score: score / 1,
          rationale: Map.get(parsed, "rationale", "")
        }

      {:ok, _} ->
        %{
          gemini_status: "error",
          gemini_error: "invalid_grade_schema",
          preserved: false,
          score: 0.0,
          gemini_body: truncate(text)
        }

      {:error, reason} ->
        %{
          gemini_status: "error",
          gemini_error: inspect(reason),
          preserved: false,
          score: 0.0,
          gemini_body: truncate(text)
        }
    end
  end

  defp extract_json_object(text) do
    case Regex.run(~r/\{.*\}/s, text) do
      [json] -> json
      nil -> text
    end
  end

  defp summarize(rows, conditions) do
    combos =
      rows
      |> Enum.group_by(& &1.combo)
      |> Enum.map(fn {combo, combo_rows} ->
        n = length(combo_rows)
        preserved = Enum.count(combo_rows, &(&1.preserved == true))

        %{
          combo: combo,
          n: n,
          preserved: preserved,
          rate: safe_div(preserved, n),
          local_errors: Enum.count(combo_rows, &(&1.local_status == "error")),
          grader_errors: Enum.count(combo_rows, &(&1.gemini_status == "error"))
        }
      end)

    %{
      kind: "synthetic_contrastive_canary_summary",
      measured_at: DateTime.utc_now() |> DateTime.to_iso8601(),
      condition_hash: conditions.condition_hash,
      conditions: conditions,
      combos: combos
    }
  end

  defp conditions(gemini_model, combos) do
    base = %{
      hive_revision: git_revision(Path.expand("..", __DIR__)),
      swarm_revision: git_revision(Path.expand("../../swarm", __DIR__)),
      hive_dirty?: git_dirty?(Path.expand("..", __DIR__)),
      swarm_dirty?: git_dirty?(Path.expand("../../swarm", __DIR__)),
      swarm_env: System.get_env("SWARM_ENV", ""),
      database: System.get_env("SWARM_DB_NAME", ""),
      ml_address: System.get_env("SWARM_ML_ADDRESS", ""),
      gemini_model: gemini_model,
      combos: Enum.map(combos, &combo_label/1),
      fixtures: Enum.map(@fixtures, & &1.id)
    }

    hash =
      base
      |> Jason.encode!()
      |> then(&:crypto.hash(:sha256, &1))
      |> Base.encode16(case: :lower)

    Map.put(base, :condition_hash, hash)
  end

  defp combos do
    raw =
      System.get_env("CONTRAST_COMBOS") ||
        default_combo()

    raw
    |> String.split(",", trim: true)
    |> Enum.map(fn spec ->
      case String.split(spec, "=>", parts: 2) do
        [panel, judge] ->
          %{panel: String.split(panel, "+", trim: true), judge: String.trim(judge)}

        _ ->
          raise "invalid CONTRAST_COMBOS entry #{inspect(spec)}; expected panelA+panelB=>judge"
      end
    end)
  end

  defp default_combo do
    panel = System.get_env("SWARM_CONSILIUM_PANEL", "qwen3:14b,lfm2.5:8b")
    judge = System.get_env("SWARM_CONSILIUM_JUDGE", "gemma4:31b")
    String.replace(panel, ",", "+") <> "=>" <> judge
  end

  defp enforce_model_policy!(combos, allowlisted_models) do
    models = combos |> Enum.flat_map(fn combo -> [combo.judge | combo.panel] end) |> Enum.uniq()
    unlisted = Enum.reject(models, &MapSet.member?(allowlisted_models, &1))

    if unlisted != [] do
      raise "refusing unlisted model(s): #{Enum.join(unlisted, ", ")}"
    end

    if System.get_env("CONTRAST_ALLOW_MODEL_LOAD") != "true" do
      resident = resident_models()
      missing = Enum.reject(models, &MapSet.member?(resident, &1))

      if missing != [] do
        raise """
        refusing to load non-resident model(s): #{Enum.join(missing, ", ")}
        Set CONTRAST_ALLOW_MODEL_LOAD=true only for an explicit measurement.
        """
      end
    end
  end

  defp resident_models do
    case System.cmd("docker", ["exec", "hive-ollama-1", "ollama", "ps"], stderr_to_stdout: true) do
      {out, 0} ->
        out
        |> String.split("\n", trim: true)
        |> Enum.drop(1)
        |> Enum.map(fn line -> line |> String.split(~r/\s+/, trim: true) |> List.first() end)
        |> Enum.reject(&is_nil/1)
        |> MapSet.new()

      {out, status} ->
        raise "could not inspect hive-ollama-1 residency (exit #{status}): #{out}"
    end
  end

  defp start_swarm! do
    {:ok, _} = Application.ensure_all_started(:swarm)
  end

  defp await_ml_pool!(deadline_ms \\ 5_000) do
    deadline = System.monotonic_time(:millisecond) + deadline_ms
    await_ml_pool_until!(deadline)
  end

  defp await_ml_pool_until!(deadline) do
    case ChannelPool.checkout() do
      {:ok, _channel, _worker} ->
        :ok

      {:error, :unavailable} ->
        if System.monotonic_time(:millisecond) >= deadline do
          raise "ML channel pool did not become healthy within the canary startup budget"
        else
          Process.sleep(100)
          await_ml_pool_until!(deadline)
        end
    end
  end

  defp out_path do
    System.get_env("CANARY_OUT") ||
      Path.expand("../tmp/contrastive-canary/latest.jsonl", __DIR__)
  end

  defp trend_path do
    System.get_env("CANARY_TREND") ||
      Path.expand("../tmp/contrastive-canary/trend.jsonl", __DIR__)
  end

  defp write_jsonl!(path, rows) do
    File.mkdir_p!(Path.dirname(path))
    File.write!(path, Enum.map_join(rows, "\n", &Jason.encode!/1) <> "\n")
  end

  defp append_jsonl!(path, rows) do
    File.mkdir_p!(Path.dirname(path))
    File.write!(path, Enum.map_join(rows, "\n", &Jason.encode!/1) <> "\n", [:append])
  end

  defp require_env!(name) do
    case System.get_env(name) do
      nil -> raise "#{name} is required"
      "" -> raise "#{name} is required"
      value -> value
    end
  end

  defp combo_label(%{panel: panel, judge: judge}), do: Enum.join(panel, "+") <> "=>" <> judge

  defp git_revision(path), do: git(path, ["rev-parse", "HEAD"], "unknown")

  defp git_dirty?(path), do: git(path, ["status", "--porcelain"], "") != ""

  defp git(path, args, fallback) do
    case System.cmd("git", ["-C", path | args], stderr_to_stdout: true) do
      {out, 0} -> String.trim(out)
      _ -> fallback
    end
  end

  defp safe_div(_num, 0), do: 0.0
  defp safe_div(num, den), do: num / den

  defp truncate(text) when is_binary(text), do: String.slice(text, 0, 500)
end

:inets.start()
:ssl.start()
SyntheticContrastiveCanary.run()
