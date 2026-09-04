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
