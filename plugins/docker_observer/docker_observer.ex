defmodule Hive.Docker.Observer do
  @moduledoc """
  The local Docker daemon as a **control-plane observer** (workspace ADR-22). Small on
  purpose: it exists to answer one thing the in-container observer measurably cannot.

  ## Why it exists, from a measurement rather than a plan

  Running the POSIX observer inside `hive-kernel-1` established that **a container cannot
  self-report a continuant**: no `/etc/machine-id`, `/proc/self/cgroup` gives `0::/` under
  cgroup v2, and its hostname is the container id, which changes on recreate. What
  survives a recreate — the compose project and service — is known to the orchestrator and
  to nothing inside.

  So the daemon supplies the missing half. It observes that container `7a76fc60ca81`
  belongs to service `hive/kernel`, and the container itself reports `7a76fc60ca81` as its
  hostname. Those two literal observations meet on the same string. **Neither observer
  infers the link; they state the two halves of it.**

  ## Same profile, different observer

  Like the Kubernetes control plane, this is an observer and not a transport — a transport
  carries the POSIX observer *into* a container, this describes the containers from
  outside. It shares the observation-run envelope and nothing else, which is the boundary
  the profile was scoped to.

  Executor is `Hive.Posix.Transport` shaped (argv in, bytes out), so `Transport.Local`
  serves it unchanged.
  """

  alias Hive.Posix.Transport

  @classes [:daemon_identity, :containers]

  @doc "The observation classes this observer knows, in a stable order."
  @spec classes() :: [atom()]
  def classes, do: @classes

  @doc "The reads a class needs. Read-only `docker` subcommands, literal argv."
  @spec reads(atom()) :: [Transport.read()]
  def reads(:daemon_identity), do: [~w(docker system info --format {{.ID}})]
  def reads(:containers), do: [~w(docker ps --no-trunc=false --format {{json_placeholder}})]

  @doc "Every read this observer makes."
  @spec allowlist() :: [{atom(), Transport.read()}]
  def allowlist, do: for(class <- @classes, read <- reads(class), do: {class, read})

  @doc "Ask one class through `executor` (a `Hive.Posix.Transport`)."
  @spec observe(atom(), module(), keyword()) :: map()
  def observe(class, executor \\ Transport.Local, opts \\ [])

  def observe(class, executor, opts) when class in @classes do
    [read] = reads(class)

    case executor.exec(argv(read), Keyword.put(opts, :read_id, "#{class}")) do
      {:ok, %{output: out, exit_status: 0}} -> parse(class, out, read)
      {:ok, %{exit_status: 127}} -> blank(class, :unsupported, read, "docker is not present (127)")
      {:ok, %{exit_status: s}} -> blank(class, :partial, read, "exit status #{s}")
      {:error, :not_executable} -> blank(class, :unsupported, read, "docker is not present")
      {:error, reason} -> blank(class, :partial, read, "executor error: #{inspect(reason)}")
    end
  end

  # `{{json .}}` cannot survive Elixir's ~w sigil intact, so the declaration holds a
  # placeholder and it is substituted here. The declared read and the executed read must
  # stay the same string, which is why this is one function and not two literals.
  defp argv(read), do: Enum.map(read, &String.replace(&1, "{{json_placeholder}}", "{{json .}}"))

  defp parse(:daemon_identity, out, read) do
    case String.trim(out) do
      "" -> blank(:daemon_identity, :partial, read, "daemon reported no id")
      id -> ok(:daemon_identity, [%{relation: "self_reports_daemon_id", object: id}], read, [])
    end
  end

  defp parse(:containers, out, read) do
    {artifacts, skips} =
      out
      |> String.split("\n", trim: true)
      |> Enum.reduce({[], []}, fn line, {ok_acc, bad} ->
        case Jason.decode(line) do
          {:ok, %{"ID" => id} = c} when is_binary(id) and id != "" ->
            {container_facts(id, c) ++ ok_acc, bad}

          _ ->
            {ok_acc, [%{reason: "container line did not parse or had no id"} | bad]}
        end
      end)

    ok(:containers, Enum.reverse(artifacts), read, Enum.reverse(skips))
  end

  # Two literal facts per container. The first is the incarnation, and it is the SAME
  # string the container reports as its own hostname -- which is what lets the two
  # observers meet without either of them guessing.
  defp container_facts(id, c) do
    labels = parse_labels(Map.get(c, "Labels", ""))
    project = labels["com.docker.compose.project"]
    service = labels["com.docker.compose.service"]

    base = [%{relation: "has_container", object: id}]

    if is_binary(project) and is_binary(service) do
      [%{relation: "container_of_service", object: "#{id} in #{project}/#{service}"} | base]
    else
      base
    end
  end

  defp parse_labels(""), do: %{}

  defp parse_labels(s) do
    s
    |> String.split(",")
    |> Enum.flat_map(fn kv ->
      case String.split(kv, "=", parts: 2) do
        [k, v] -> [{k, v}]
        _ -> []
      end
    end)
    |> Map.new()
  end

  defp ok(class, artifacts, read, skips),
    do: %{class: class, status: :complete, artifacts: artifacts, reads: [read], skips: skips, detail: nil}

  defp blank(class, status, read, detail),
    do: %{class: class, status: status, artifacts: [], reads: [read], skips: [], detail: detail}

  @doc "Fold identity artifacts into the map the connector keys environments on."
  @spec identity_map(map()) :: %{optional(atom()) => String.t()}
  def identity_map(%{class: :daemon_identity, artifacts: artifacts}),
    do: Map.new(artifacts, fn %{relation: "self_reports_daemon_id", object: v} -> {:daemon_id, v} end)

  def identity_map(_), do: %{}
end
