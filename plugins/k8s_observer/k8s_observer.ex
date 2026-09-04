defmodule Hive.K8s.Observer do
  @moduledoc """
  The Kubernetes **control-plane observer** (workspace ADR-22). Answers *what exists and
  where*, from the API, with no shell anywhere.

  ## It is an observer, not a transport

  Kubernetes appears exactly once in this design. `kubectl exec` into a pod is a
  *transport* belonging to the POSIX observer — a pod is just another Unix-like
  environment. The control plane is this, a different observer with its own reads. Round
  after round, conflating the two is the mistake that "everything is an adapter" invites.

  ## Its reads are API calls, and that is the point

  A POSIX read is argv. A read here is `%{verb:, api_group:, resource:, resource_names:}`
  — which is *exactly the shape an RBAC rule takes*. So the same property the SSH
  allowlist has holds here: `hive/scripts/k8s_observer_rbac.exs` generates the
  ServiceAccount, ClusterRole and binding **from this declaration**, and a cluster
  administrator grants precisely what the code can ask for.

  This is also where ADR-22's open question (c) gets its answer: the two observers do
  **not** share a read type, and they were never meant to. What they share is the
  observation-run envelope. The profile holds because it was scoped to the envelope rather
  than to the reads.

  ## IT HAS NEVER CONTACTED A CLUSTER

  There is a kubeconfig on the build machine that reaches real production with
  cluster-admin credentials, including a different client's infrastructure. It was not
  used — not for a read, not for a version check, not to test reachability. The default
  client below therefore **refuses**: this observer cannot contact anything unless a
  caller passes an explicit client, which no default path does.

  Untested is an acceptable state. Quietly tested against production admin is not.
  """

  defmodule Client do
    @moduledoc """
    The executor for this observer: it performs one API read and returns the body.

    A MODULE, not a function, and deliberately the same shape as a POSIX transport —
    writing the second observer is what showed why. The connector records
    `executor.name()` in every envelope, so an executor that is a bare closure has no name
    to record and the envelope silently loses the field that says how the observation was
    made. Both observers therefore take an executor module; only the READ TYPE differs,
    which is exactly the boundary the profile was scoped to.
    """

    @callback name() :: String.t()
    @callback read(read :: map(), opts :: keyword()) :: {:ok, map()} | {:error, term()}
  end

  defmodule Client.Refusing do
    @moduledoc """
    The default executor. It refuses.

    A default that reached for an ambient kubeconfig would mean the difference between
    "designed" and "ran against production" was one forgotten argument. On the machine
    this was built on, that kubeconfig holds cluster-admin credentials for real production
    and for a different client's infrastructure. So the default refuses, and contacting a
    cluster takes a deliberate act.
    """

    @behaviour Hive.K8s.Observer.Client

    @impl true
    def name, do: "refusing"

    @impl true
    def read(_read, _opts), do: {:error, :no_client_configured}
  end

  @typedoc "One API read. Shaped like an RBAC rule because it becomes one."
  @type read :: %{
          verb: String.t(),
          api_group: String.t(),
          resource: String.t(),
          resource_names: [String.t()]
        }

  @classes [:cluster_identity, :workloads, :pod_placement]

  @doc "The observation classes this observer knows, in a stable order."
  @spec classes() :: [atom()]
  def classes, do: @classes

  @doc """
  The reads a class needs. This list IS the RBAC ask.

  `resource_names` is populated wherever the observer needs one named object rather than a
  kind — a `get` on exactly `kube-system` is a far smaller grant than `list namespaces`,
  and the artifact should ask for the smaller one.
  """
  @spec reads(atom()) :: [read()]
  def reads(:cluster_identity),
    do: [
      %{verb: "get", api_group: "", resource: "namespaces", resource_names: ["kube-system"]}
    ]

  def reads(:workloads),
    do: [
      %{verb: "list", api_group: "apps", resource: "deployments", resource_names: []},
      %{verb: "list", api_group: "apps", resource: "statefulsets", resource_names: []},
      %{verb: "list", api_group: "apps", resource: "daemonsets", resource_names: []}
    ]

  def reads(:pod_placement),
    do: [%{verb: "list", api_group: "", resource: "pods", resource_names: []}]

  @doc "Every read, for the RBAC generator. The complete ask, not a summary of it."
  @spec allowlist() :: [{atom(), read()}]
  def allowlist, do: for(class <- @classes, read <- reads(class), do: {class, read})

  @doc """
  Ask one class through `client`.

  `client` is a `Client` module. There is deliberately no usable default: see
  `Client.Refusing`.
  """
  @spec observe(atom(), module(), keyword()) :: map()
  def observe(class, client \\ Client.Refusing, opts \\ [])

  def observe(class, client, opts) when class in @classes do
    results = Enum.map(reads(class), fn r -> {r, client.read(r, opts)} end)

    cond do
      Enum.all?(results, &match?({_, {:error, :no_client_configured}}, &1)) ->
        # We never asked. That is NOT the cluster being unable to answer, and labelling it
        # `unsupported` would record our own omission as a fact about the cluster.
        blank(class, :partial, "no client configured; nothing was contacted")

      Enum.all?(results, &match?({_, {:error, :forbidden}}, &1)) ->
        # RBAC declined every read in this class. The cluster cannot answer it for us,
        # which is `unsupported` -- it closes nothing, exactly like a missing binary.
        blank(class, :unsupported, "every read in this class was forbidden by RBAC")

      Enum.any?(results, &match?({_, {:error, _}}, &1)) ->
        # Some worked, some did not. Partial closes nothing.
        blank(class, :partial, "at least one read failed")

      true ->
        parse(class, Enum.map(results, fn {_r, {:ok, body}} -> body end))
    end
  end

  # --- parsing ---------------------------------------------------------------------

  defp parse(:cluster_identity, [ns]) do
    uid = get_in(ns, ["metadata", "uid"])

    if is_binary(uid) and uid != "" do
      artifacts(:cluster_identity, [%{relation: "self_reports_cluster_uid", object: uid}])
    else
      blank(:cluster_identity, :partial, "kube-system carried no uid")
    end
  end

  defp parse(:workloads, bodies) do
    bodies
    |> Enum.flat_map(fn body ->
      body
      |> Map.get("items", [])
      |> Enum.flat_map(fn item ->
        kind = item |> Map.get("kind", inferred_kind(body)) |> to_string()
        ns = get_in(item, ["metadata", "namespace"])
        name = get_in(item, ["metadata", "name"])

        if is_binary(ns) and is_binary(name) and name != "",
          do: [%{relation: "has_workload", object: "#{ns}/#{kind}/#{name}"}],
          else: []
      end)
    end)
    |> then(&artifacts(:workloads, &1))
  end

  defp parse(:pod_placement, [body]) do
    body
    |> Map.get("items", [])
    |> Enum.flat_map(fn pod ->
      ns = get_in(pod, ["metadata", "namespace"])
      name = get_in(pod, ["metadata", "name"])
      node = get_in(pod, ["spec", "nodeName"])

      # A pod not yet scheduled has no node. That is not a placement fact, and inventing
      # one would be exactly the inference this profile forbids.
      if is_binary(ns) and is_binary(name) and is_binary(node) and node != "",
        do: [%{relation: "has_pod_on_node", object: "#{ns}/#{name} on #{node}"}],
        else: []
    end)
    |> then(&artifacts(:pod_placement, &1))
  end

  # A list response names its own kind as e.g. "DeploymentList".
  defp inferred_kind(body) do
    body |> Map.get("kind", "") |> to_string() |> String.replace_suffix("List", "")
  end

  defp artifacts(class, artifacts),
    do: %{
      class: class,
      status: :complete,
      artifacts: artifacts,
      reads: reads(class),
      skips: [],
      detail: nil
    }

  defp blank(class, status, detail),
    do: %{class: class, status: status, artifacts: [], reads: reads(class), skips: [], detail: detail}

  @doc "Fold identity artifacts into the map the connector keys environments on."
  @spec identity_map(map()) :: %{optional(atom()) => String.t()}
  def identity_map(%{class: :cluster_identity, artifacts: artifacts}) do
    Map.new(artifacts, fn %{relation: "self_reports_cluster_uid", object: v} -> {:cluster_uid, v} end)
  end

  def identity_map(_), do: %{}
end
