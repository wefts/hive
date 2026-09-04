defmodule Hive.Posix.Transport do
  @moduledoc """
  HOW to reach an environment (workspace ADR-22). A transport executes one read and
  returns its bytes. It knows nothing about what the bytes mean.

  This is one half of the observer/transport split: the POSIX observer owns the knowledge
  of *what to ask*, and owns its transports as interchangeable ways of asking. `kubectl
  exec` carrying that observer into a pod is a transport; Kubernetes-the-control-plane is
  a different observer entirely, not a transport.

  v1 ships `Local` only. The behaviour exists now so the seam is exercised by the first
  implementation rather than retrofitted around the second.

  ## Enforcement lives here, not in the observer

  The *set* of reads is a property of what we want to know, so it belongs to the observer.
  *Enforcement* is a property of the channel and belongs here: a forced command in
  `authorized_keys` for SSH, RBAC for `kubectl exec`, nothing for local. A transport may
  refuse a read; it may never invent one.
  """

  @typedoc "A read: argv, never a shell string — there is no shell to quote into."
  @type read :: [String.t()]

  @typedoc """
  `:ok` carries the captured stdout and the exit status. `{:error, :not_executable}` is
  the transport saying the binary is absent — distinct from a command that ran and failed,
  because only the former means the environment cannot answer this class at all.
  """
  @type result ::
          {:ok, %{output: String.t(), exit_status: integer()}}
          | {:error, :not_executable | :refused | term()}

  @doc "Human name of this transport, recorded in the run envelope."
  @callback name() :: String.t()

  @doc "Execute one read in the target environment."
  @callback exec(read(), opts :: keyword()) :: result()

  # NOTE, from building v1: there is deliberately NO `self_identity` callback.
  #
  # The first draft put one here, and it was a leak. Reading `/etc/machine-id` is a
  # question about a Unix-like OS -- *what to ask* -- so it belongs to the observer, and
  # putting it on the transport gave every future transport a second thing to reimplement.
  # That is exactly the duplication the split exists to prevent, reappearing inside the
  # split. Identity is now an observation class like any other, and a transport does one
  # thing: run a read.
end

defmodule Hive.Posix.Transport.Local do
  @moduledoc """
  The local transport: run the read in the process's own environment.

  Zero access risk and nothing to negotiate, which is why v1 uses it — and why v1 can be
  built and measured before any access grant exists.
  """

  @behaviour Hive.Posix.Transport

  @impl true
  def name, do: "local"

  @impl true
  def exec([bin | args], opts) do
    timeout = Keyword.get(opts, :timeout_ms, 10_000)

    task =
      Task.async(fn ->
        try do
          {out, status} = System.cmd(bin, args, stderr_to_stdout: false)
          {:ok, %{output: out, exit_status: status}}
        rescue
          # `System.cmd` raises when the binary does not exist. That is the environment
          # saying "I cannot answer this", not a failed run, and the caller must be able
          # to tell those apart.
          ErlangError -> {:error, :not_executable}
        end
      end)

    case Task.yield(task, timeout) || Task.shutdown(task, :brutal_kill) do
      {:ok, result} -> result
      _ -> {:error, :timeout}
    end
  end

  def exec([], _opts), do: {:error, :empty_read}

end

defmodule Hive.Posix.Transport.Ssh do
  @moduledoc """
  Reach a remote environment over SSH, read-only, with **server-side** enforcement.

  ## It sends an id, not a command

  The transport never sends a command line. It sends the read's **id**
  (`Hive.Posix.Observer.read_id/2`), and the remote side looks that id up in a fixed
  table or refuses. There is therefore nothing on the wire to quote, escape or inject
  into, and the remote never parses anything a caller controls.

  ## The boundary is on the server, not here

  An allowlist in this file would be advice. The boundary is
  `command="…/swarm-observe" ` in the remote account's `authorized_keys`: sshd then runs
  the wrapper **whatever** the client asks for, and puts the client's request in
  `SSH_ORIGINAL_COMMAND` for the wrapper to accept or refuse. A client that asks for
  anything else gets the wrapper anyway, and the wrapper refuses.

  Generate the wrapper and the `authorized_keys` line with
  `hive/scripts/posix_observer_allowlist.exs` — both are derived from the observer's own
  declaration, so what is granted is exactly what the code can ask for.

  ## What this transport cannot establish

  Reaching a host and reading its self-identification tells you **what you reached**. It
  does not tell you that the thing you reached is the graph node you meant. That edge —
  `env:` to `net:host:` — is still missing, and no transport closes it. Recording the
  intended target beside the self-reported identity is what makes the difference
  *visible*; it is not what makes it *resolved*.
  """

  @behaviour Hive.Posix.Transport

  @impl true
  def name, do: "ssh"

  @impl true
  @doc """
  `opts`:
    * `:host` (required) — `user@host` or an ssh_config alias;
    * `:read_id` (required) — the id to send. The transport is TOLD the id, it does not
      derive one: deriving it would mean reaching into the observer's declaration, which
      is the same leak that put `self_identity` on the transport in v1;
    * `:wrapper_path` — invoke the wrapper explicitly instead of relying on a forced
      command. For exercising the chain on a host where no `command=` is installed; the
      production shape leaves this unset.
    * `:ssh_options` — extra argv for the ssh client;
    * `:timeout_ms`.
  """
  def exec(_read, opts) do
    host = Keyword.fetch!(opts, :host)
    id = Keyword.fetch!(opts, :read_id)

    remote =
      case Keyword.get(opts, :wrapper_path) do
        nil -> [id]
        path -> [path, id]
      end

    argv = ssh_options(opts) ++ [host, "--"] ++ remote

    case Hive.Posix.Transport.Local.exec(["ssh" | argv], opts) do
      {:ok, %{exit_status: 0}} = ok ->
        ok

      # The wrapper's own refusal code. Distinct from a read that ran and failed: it means
      # the server declined, which is a fact about the boundary, not about the host.
      {:ok, %{exit_status: 93}} ->
        {:error, :refused}

      # sshd could not run the command at all -- on a hardened host that is the binary
      # being absent behind the wrapper, which is the environment saying it cannot answer.
      {:ok, %{exit_status: 127}} ->
        {:error, :not_executable}

      {:ok, %{exit_status: 255}} ->
        {:error, :ssh_unreachable}

      other ->
        other
    end
  end

  # BatchMode: never prompt. A transport that can block on a passphrase prompt is a
  # transport that can hang a scheduled run.
  defp ssh_options(opts) do
    Keyword.get(opts, :ssh_options, [
      "-o",
      "BatchMode=yes",
      "-o",
      "ConnectTimeout=5",
      "-o",
      "StrictHostKeyChecking=accept-new"
    ])
  end
end
