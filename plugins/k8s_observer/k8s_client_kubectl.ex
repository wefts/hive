defmodule Hive.K8s.Client.Kubectl do
  @moduledoc """
  A real executor for `Hive.K8s.Observer`, via the `kubectl` binary.

  ## It will not use an ambient kubeconfig

  `:kubeconfig` is **required**. There is no fallback to `KUBECONFIG` or `~/.kube/config`,
  and that is deliberate rather than fussy: a developer machine's default kubeconfig
  routinely holds cluster-admin contexts for production, and for other people's
  production. An executor that silently reaches for it turns "I forgot an argument" into
  "an agent read a customer's cluster with a human's admin credential". Passing the path
  is the act that makes the target a decision.

  Prefer a ServiceAccount token scoped by the generated RBAC
  (`hive/scripts/k8s_observer_rbac.exs`) over any personal credential.

  ## It only ever reads

  The argv is built from the observer's own read declaration — `get` or `list` on a named
  resource, `-o json`, nothing else. There is no code path here that writes, and no place
  for a caller-supplied string to become part of a command.
  """

  @behaviour Hive.K8s.Observer.Client

  @impl true
  def name, do: "kubectl"

  @impl true
  def read(%{verb: verb} = read, opts) when verb in ["get", "list"] do
    kubeconfig = Keyword.fetch!(opts, :kubeconfig)
    timeout = Keyword.get(opts, :timeout_ms, 20_000)

    argv =
      ["--kubeconfig", kubeconfig, "get", qualified(read)] ++
        names(read) ++ scope_flag(read) ++ ["-o", "json"]

    task = Task.async(fn -> System.cmd("kubectl", argv, stderr_to_stdout: true) end)

    case Task.yield(task, timeout) || Task.shutdown(task, :brutal_kill) do
      {:ok, {out, 0}} -> decode(out)
      {:ok, {out, _}} -> {:error, classify(out)}
      _ -> {:error, :timeout}
    end
  end

  def read(%{verb: verb}, _opts), do: {:error, {:refusing_non_read_verb, verb}}

  # `resource.group` disambiguates against CRDs that share a short name.
  defp qualified(%{resource: r, api_group: ""}), do: r
  defp qualified(%{resource: r, api_group: g}), do: "#{r}.#{g}"

  defp names(%{resource_names: []}), do: []
  defp names(%{resource_names: names}), do: names

  # A named object is fetched directly; an unnamed list spans namespaces, because the
  # observer's question is about the cluster and not about one namespace in it.
  defp scope_flag(%{resource_names: []}), do: ["--all-namespaces"]
  defp scope_flag(_), do: []

  defp decode(out) do
    case Jason.decode(out) do
      {:ok, body} -> {:ok, body}
      _ -> {:error, :unparseable_response}
    end
  end

  # RBAC refusal must be distinguishable from any other failure: the observer turns a
  # class where every read was forbidden into `unsupported`, and everything else into
  # `partial`. Collapsing them would let a network blip look like a permissions boundary.
  defp classify(out) do
    cond do
      out =~ "forbidden" or out =~ "Forbidden" -> :forbidden
      out =~ "Unauthorized" -> :unauthorized
      out =~ "NotFound" or out =~ "not found" -> :not_found
      true -> :kubectl_error
    end
  end
end
