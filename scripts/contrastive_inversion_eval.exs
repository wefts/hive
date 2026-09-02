# Contrastive-inversion eval harness.
#
# Pulls candidate contrastive sentences from the configured Swarm DB at run time
# and, optionally, evaluates panel/judge combinations against those sentences.
# Outputs go to hive/tmp by default because real corpus text is private runtime
# data and must never be committed.
#
# Default extraction-only run:
#
#   cd swarm/kernel
#   eval "$(cd ../../hive && SWARM_ENV=staging scripts/kernel-measure-env)"
#   CONTRAST_LIMIT=40 MIX_ENV=dev \
#     mise exec -- mix run --no-start ../../hive/scripts/contrastive_inversion_eval.exs
#
# Optional model run, refusing to load models that are not already resident:
#
#   CONTRAST_EVALUATE=true \
#   CONTRAST_COMBOS='qwen3:14b+lfm2.5:8b=>gemma4:31b' \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/contrastive_inversion_eval.exs
#
# To measure a non-default candidate explicitly, the caller must opt in and
# accept model loading:
#
#   CONTRAST_ALLOW_UNLISTED_MODELS=true CONTRAST_ALLOW_MODEL_LOAD=true \
#   CONTRAST_COMBOS='qwen3:14b+lfm2.5:8b=>granite4.1-guardian:8b' ...

require Logger

Logger.configure(level: :warning)

defmodule ContrastiveInversionEval do
  @moduledoc false

  alias Swarm.Consilium
  alias Swarm.ML.ChannelPool
  alias Swarm.Repo

  @sentence_re ~r/(?<=[.!?])\s+(?=[[:upper:]0-9`"'])/u
  @default_patterns ["differs from", "unlike", "whereas"]
  @pattern_regexes %{
    "differs from" => ~r/\bdiffers\s+from\b/i,
    "unlike" => ~r/\bunlike\b/i,
    "whereas" => ~r/\bwhereas\b/i
  }
  @postgres_regexes %{
    "differs from" => "\\mdiffers\\s+from\\M",
    "unlike" => "\\munlike\\M",
    "whereas" => "\\mwhereas\\M"
  }
  # Campaign model allowlist. Granite is intentionally included only as the approved
  # third-family judge candidate; banned large defaults still require an explicit override.
  @allowlisted_models MapSet.new([
                        "gemma4:31b",
                        "qwen3:14b",
                        "lfm2.5:8b",
                        "bge-m3",
                        "granite4.1-guardian:8b"
                      ])

  def run do
    Application.ensure_all_started(:ecto_sql)
    Application.ensure_all_started(:postgrex)
    {:ok, _} = Repo.start_link()

    patterns = env_list("CONTRAST_PATTERNS", @default_patterns)
    limit = env_int("CONTRAST_LIMIT", 40)
    per_chunk = env_int("CONTRAST_SENTENCES_PER_CHUNK", 3)
    scopes = scopes()
    out_path = output_path()

    candidates =
      patterns
      |> pull_chunks(scopes, max(limit * 4, limit))
      |> extract_sentences(patterns, per_chunk)
      |> Enum.take(limit)

    File.mkdir_p!(Path.dirname(out_path))

    evaluate? = env_bool("CONTRAST_EVALUATE")

    rows =
      if evaluate? do
        combos = combos()
        enforce_model_policy!(combos, @allowlisted_models)
        start_ml_pool!()
        await_ml_pool!()
        evaluate(candidates, combos)
      else
        Enum.map(candidates, &Map.put(&1, :kind, :candidate))
      end

    write_jsonl!(out_path, rows)

    IO.puts("contrastive-inversion: db=#{current_db()} candidates=#{length(candidates)}")
    IO.puts("contrastive-inversion: patterns=#{Enum.join(patterns, ", ")}")
    IO.puts("contrastive-inversion: evaluate=#{evaluate?} rows=#{length(rows)}")
    IO.puts("contrastive-inversion: wrote #{out_path}")
  end

  defp pull_chunks(patterns, scopes, chunk_limit) do
    where =
      patterns
      |> Enum.with_index(1)
      |> Enum.map(fn {_pattern, i} -> "ch.text ~* $#{i}" end)
      |> Enum.join(" OR ")

    params =
      Enum.map(patterns, &regex_source/1) ++
        [scopes, chunk_limit]

    scope_param = length(patterns) + 1
    limit_param = length(patterns) + 2

    sql = """
    SELECT ch.id, ch.node_id, ch.ordinal, ch.text, c.source_ref,
           coalesce(nullif(n.provenance->>'display_key', ''), n.key) AS title,
           n.scope
      FROM chunk ch
      JOIN node n ON n.id = ch.node_id
      LEFT JOIN content c ON c.node_id = ch.node_id
     WHERE n.scope = ANY($#{scope_param}::text[])
       AND (#{where})
     ORDER BY ch.id, ch.ordinal
     LIMIT $#{limit_param}
    """

    Repo.query!(sql, params)
    |> Map.fetch!(:rows)
    |> Enum.map(fn [chunk_id, node_id, ordinal, text, source_ref, title, scope] ->
      %{
        chunk_id: chunk_id,
        node_id: node_id,
        ordinal: ordinal,
        text: text,
        source_ref: source_ref,
        title: title,
        scope: scope
      }
    end)
  end

  defp extract_sentences(chunks, patterns, per_chunk) do
    chunks
    |> Enum.flat_map(fn chunk ->
      chunk.text
      |> split_sentences()
      |> Enum.filter(&matches_any?(&1, patterns))
      |> Enum.take(per_chunk)
      |> Enum.map(fn sentence ->
        marker = Enum.find(patterns, &matches_pattern?(sentence, &1))

        %{
          kind: :candidate,
          id: candidate_id(chunk.chunk_id, sentence),
          marker: marker,
          sentence: String.trim(sentence),
          chunk_id: chunk.chunk_id,
          node_id: chunk.node_id,
          ordinal: chunk.ordinal,
          source_ref: chunk.source_ref,
          title: chunk.title,
          scope: chunk.scope
        }
      end)
    end)
  end

  defp split_sentences(text) do
    text
    |> String.replace(~r/\s+/u, " ")
    |> String.split(@sentence_re, trim: true)
  end

  defp evaluate(candidates, combos) do
    for candidate <- candidates,
        combo <- combos do
      started_at = System.monotonic_time(:millisecond)

      result =
        Consilium.deliberate(question(candidate),
          grounding: grounding(candidate),
          fleet: %{panel: combo.panel, judge: combo.judge, token_ceiling: 8_000}
        )

      duration_ms = System.monotonic_time(:millisecond) - started_at

      base = %{
        kind: :evaluation,
        id: candidate.id,
        marker: candidate.marker,
        sentence: candidate.sentence,
        source_ref: candidate.source_ref,
        title: candidate.title,
        chunk_id: candidate.chunk_id,
        combo: %{panel: combo.panel, judge: combo.judge},
        duration_ms: duration_ms
      }

      case result do
        {:ok, verdict} ->
          Map.merge(base, %{
            status: :ok,
            answer: verdict.answer,
            confidence: verdict.confidence,
            supported: verdict.supported,
            disagreement: verdict.disagreement,
            panel: verdict.panel
          })

        {:error, reason} ->
          Map.merge(base, %{status: :error, error: inspect(reason)})
      end
    end
  end

  defp question(candidate) do
    """
    Preserve the direction of the contrast in the source sentence.
    Which side has the capability, property, recommendation, or limitation being contrasted?
    Name both sides when the sentence names them, and say if the source is insufficient.

    Source sentence marker: #{candidate.marker}
    """
  end

  defp grounding(candidate) do
    """
    Source: #{candidate.title || candidate.source_ref || "unknown"}

    #{candidate.sentence}
    """
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
          panel_models = panel |> String.split("+", trim: true) |> Enum.map(&String.trim/1)
          %{panel: panel_models, judge: String.trim(judge)}

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
    models =
      combos
      |> Enum.flat_map(fn combo -> [combo.judge | combo.panel] end)
      |> Enum.uniq()

    if System.get_env("CONTRAST_ALLOW_UNLISTED_MODELS") != "true" do
      unlisted = Enum.reject(models, &MapSet.member?(allowlisted_models, &1))

      if unlisted != [] do
        raise """
        refusing unlisted model(s): #{Enum.join(unlisted, ", ")}
        Set CONTRAST_ALLOW_UNLISTED_MODELS=true only for an explicit operator-approved candidate.
        """
      end
    end

    if System.get_env("CONTRAST_ALLOW_MODEL_LOAD") != "true" do
      resident = resident_models()
      missing = Enum.reject(models, &MapSet.member?(resident, &1))

      if missing != [] do
        raise """
        refusing to load non-resident model(s): #{Enum.join(missing, ", ")}
        Start or prewarm them deliberately, or set CONTRAST_ALLOW_MODEL_LOAD=true for this measurement.
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

  defp start_ml_pool! do
    Application.ensure_all_started(:grpc)

    case DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor) do
      {:ok, _} -> :ok
      {:error, {:already_started, _}} -> :ok
    end

    case Process.whereis(ChannelPool) do
      nil ->
        {:ok, _} = ChannelPool.start_link()
        :ok

      _pid ->
        :ok
    end
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
          raise "ML channel pool did not become healthy within the eval startup budget"
        else
          Process.sleep(100)
          await_ml_pool_until!(deadline)
        end
    end
  end

  defp scopes do
    case env_list("SCOPES", []) do
      [] ->
        rows = Repo.query!("SELECT 'src:' || id::text FROM source ORDER BY id", []).rows
        ["public" | List.flatten(rows)]

      given ->
        given
    end
  end

  defp output_path do
    case System.get_env("CONTRAST_OUT") do
      nil ->
        stamp = DateTime.utc_now() |> DateTime.to_iso8601(:basic) |> String.replace("Z", "")

        __ENV__.file
        |> Path.dirname()
        |> Path.join("../tmp/contrastive-inversion/#{stamp}.jsonl")
        |> Path.expand()

      path ->
        path
    end
  end

  defp write_jsonl!(path, rows) do
    body =
      rows
      |> Enum.map(&Jason.encode!/1)
      |> Enum.join("\n")

    File.write!(path, body <> if(body == "", do: "", else: "\n"))
  end

  defp current_db do
    Repo.query!("SELECT current_database()", []).rows |> hd() |> hd()
  end

  defp env_list(name, default) do
    case System.get_env(name) do
      nil -> default
      "" -> default
      value -> value |> String.split(",", trim: true) |> Enum.map(&String.trim/1)
    end
  end

  defp env_int(name, default) do
    case System.get_env(name) do
      nil -> default
      "" -> default
      value -> String.to_integer(value)
    end
  end

  defp env_bool(name), do: System.get_env(name) in ["1", "true", "yes"]

  defp matches_any?(sentence, patterns), do: Enum.any?(patterns, &matches_pattern?(sentence, &1))

  defp matches_pattern?(text, pattern) do
    regex = Map.get(@pattern_regexes, String.downcase(pattern), loose_pattern(pattern))
    Regex.match?(regex, text)
  end

  defp candidate_id(chunk_id, sentence) do
    hash = :crypto.hash(:sha256, sentence) |> Base.encode16(case: :lower) |> String.slice(0, 12)
    "chunk:#{chunk_id}:#{hash}"
  end

  defp regex_source(pattern) do
    Map.get(@postgres_regexes, String.downcase(pattern), "\\m" <> Regex.escape(pattern) <> "\\M")
  end

  defp loose_pattern(pattern) do
    ~r/(^|[^\p{L}\p{N}])#{Regex.escape(pattern)}($|[^\p{L}\p{N}])/iu
  end
end

ContrastiveInversionEval.run()
