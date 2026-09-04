defmodule Hive.Posix.Observer do
  @moduledoc """
  WHAT to ask a Unix-like OS (workspace ADR-22). One body of knowledge, transport-agnostic.

  This is the half of the split with the value in it: knowing that
  `systemctl list-units --type=service --state=active` answers "what runs here", and how to
  read the answer, is identical down every transport. Written once here, it cannot drift
  between a local copy, an SSH copy and a `kubectl exec` copy.

  ## Observation classes, and why there are two

  A class is a question the OS can answer, with its own reads, its own parser, and its own
  closure. Two, not one, deliberately: with a single class, per-class closure and the
  `unsupported` status would be specified but never exercised. A container with no systemd
  reports `unsupported` for `active_units` and `complete` for `listening_sockets`, which is
  exactly the shape the k8s increment will need.

  ## Honest scope of v1

  What this produces is **a history of literal runtime artifacts on one environment
  incarnation.** It is NOT service placement:

    * `has_active_unit "keycloak.service"` is not "runs Keycloak" — unit name to service
      identity is an inference this connector never makes;
    * the environment is not tied to any inventory host — `env:` and `net:host:` are
      separate keyspaces until the ADR-22 D1 precondition is checked.

  Both identity edges the document→host join needs remain missing. Nothing here closes
  that gap, and nothing downstream should read it as though it did.
  """

  alias Hive.Posix.Transport

  @typedoc """
  `complete` — the read ran and parsed; absence within this class may close facts.
  `partial` — it ran and something went wrong; closes nothing.
  `unsupported` — the environment cannot answer this class at all (no systemd, no `ss`);
  closes nothing, and is deliberately distinct from an empty `complete`, which does.
  """
  @type status :: :complete | :partial | :unsupported

  @typedoc "One observed artifact: the literal thing seen, never an interpretation of it."
  @type artifact :: %{relation: String.t(), object: String.t()}

  @typedoc "The result of asking one class of one environment."
  @type observation :: %{
          class: atom(),
          status: status(),
          artifacts: [artifact()],
          reads: [Transport.read()],
          skips: [map()],
          detail: String.t() | nil
        }

  @classes [:self_identity, :active_units, :listening_sockets]

  @doc "The observation classes this observer knows, in a stable order."
  @spec classes() :: [atom()]
  def classes, do: @classes

  @doc """
  The reads a class needs, as argv. This list IS the security ask: it is what an SSH
  allowlist or a kubectl RBAC rule must permit, and it is a property of the observer, not
  of any transport (ADR-22).
  """
  @spec reads(atom()) :: [Transport.read()]
  def reads(:active_units),
    do: [~w(systemctl list-units --type=service --state=active --no-pager --output=json)]

  def reads(:listening_sockets), do: [~w(ss -H -l -tun)]

  # Identity is an observation class, not a transport capability. It moved here after v1
  # was written: having the transport read /etc/machine-id gave every future transport a
  # second thing to reimplement, which is the duplication the split exists to prevent.
  # The cost is honest and small -- two more reads on the allowlist, and a transport that
  # does exactly one thing.
  def reads(:self_identity),
    do: [
      ~w(cat /etc/machine-id),
      ~w(cat /proc/sys/kernel/random/boot_id),
      ~w(hostname),
      ~w(cat /proc/self/cgroup)
    ]

  @doc "Ask one class through `transport`. Never raises; a failure is a status, not a crash."
  @spec observe(atom(), module(), keyword()) :: observation()
  def observe(class, transport, opts \\ [])

  def observe(:self_identity, transport, opts), do: observe_identity(transport, opts)

  def observe(class, transport, opts) when class in @classes do
    [read] = reads(class)

    case transport.exec(read, opts) do
      {:ok, %{output: out, exit_status: 0}} ->
        parse(class, out, read)

      {:ok, %{exit_status: status}} ->
        # It ran and refused. That is not "nothing is here"; it closes nothing.
        blank(class, :partial, read, "exit status #{status}")

      {:error, :not_executable} ->
        # The environment cannot answer this class. Distinct from an empty answer.
        blank(class, :unsupported, read, "the binary this class needs is not present")

      {:error, reason} ->
        blank(class, :partial, read, "transport error: #{inspect(reason)}")
    end
  end

  @doc """
  The environment's own account of itself, as artifacts like any other observation.

  **Self-reported only.** Never taken from the target entry — an observer that reports
  what it was told rather than what it found cannot show "asked for A, reached something
  calling itself B" (ADR-22).
  """
  @spec observe_identity(module(), keyword()) :: observation()
  def observe_identity(transport, opts) do
    reads = reads(:self_identity)

    {artifacts, any_ok?} =
      reads
      |> Enum.zip([:machine_id, :boot_id, :hostname, :container_id])
      |> Enum.reduce({[], false}, fn {read, key}, {acc, ok?} ->
        case transport.exec(read, opts) do
          {:ok, %{output: out, exit_status: 0}} ->
            case extract(key, out) do
              nil -> {acc, ok?}
              v -> {[%{relation: "self_reports_#{key}", object: v} | acc], true}
            end

          _ ->
            {acc, ok?}
        end
      end)

    %{
      class: :self_identity,
      status: if(any_ok?, do: :complete, else: :unsupported),
      artifacts: Enum.reverse(artifacts),
      reads: reads,
      skips: [],
      detail: if(any_ok?, do: nil, else: "the environment could state nothing about itself")
    }
  end

  # Pulling a container id out of a cgroup file is knowledge about a Unix-like OS, so it
  # lives here with the rest of the observer rather than in whichever transport fetched
  # the bytes.
  defp extract(:container_id, out) do
    case Regex.scan(~r/[0-9a-f]{64}/, out) do
      [[id] | _] -> id
      _ -> nil
    end
  end

  defp extract(_key, out) do
    case String.trim(out) do
      "" -> nil
      v -> v
    end
  end

  @doc "Fold identity artifacts into the map the connector keys environments on."
  @spec identity_map(observation()) :: %{optional(atom()) => String.t()}
  def identity_map(%{class: :self_identity, artifacts: artifacts}) do
    Map.new(artifacts, fn %{relation: r, object: v} ->
      {r |> String.replace_prefix("self_reports_", "") |> String.to_atom(), v}
    end)
  end

  # --- parsing ------------------------------------------------------------------------

  defp parse(:active_units, output, read) do
    case Jason.decode(output) do
      {:ok, rows} when is_list(rows) ->
        {artifacts, skips} =
          Enum.reduce(rows, {[], []}, fn row, {ok, bad} ->
            case Map.get(row, "unit") do
              u when is_binary(u) and u != "" ->
                {[%{relation: "has_active_unit", object: u} | ok], bad}

              _ ->
                # A row we cannot name is a recorded skip, never a silent drop (ADR-21).
                {ok, [%{reason: "unit name missing or not a string"} | bad]}
            end
          end)

        %{
          class: :active_units,
          status: :complete,
          artifacts: Enum.reverse(artifacts),
          reads: [read],
          skips: Enum.reverse(skips),
          detail: nil
        }

      _ ->
        # Ran fine, output unreadable. Partial: we know nothing, so we close nothing.
        blank(:active_units, :partial, read, "output did not parse as JSON")
    end
  end

  defp parse(:listening_sockets, output, read) do
    {artifacts, skips} =
      output
      |> String.split("\n", trim: true)
      |> Enum.reduce({[], []}, fn line, {ok, bad} ->
        case String.split(line, ~r/\s+/, trim: true) do
          [proto, _state, _rq, _sq, local | _] when proto != "" and local != "" ->
            {[%{relation: "has_listening_socket", object: "#{proto} #{local}"} | ok], bad}

          _ ->
            {ok, [%{reason: "socket line did not parse"} | bad]}
        end
      end)

    %{
      class: :listening_sockets,
      status: :complete,
      artifacts: artifacts |> Enum.reverse() |> Enum.uniq(),
      reads: [read],
      skips: Enum.reverse(skips),
      detail: nil
    }
  end

  defp blank(class, status, read, detail),
    do: %{class: class, status: status, artifacts: [], reads: [read], skips: [], detail: detail}
end
