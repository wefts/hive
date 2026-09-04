# Hermetic tests for the Kubernetes control-plane observer. NO CLUSTER IS CONTACTED --
# a fake client supplies API bodies, which is the only way this observer has ever run.
# Run from swarm/kernel:
#
#   SWARM_ENV=test mise exec -- mix run --no-start \
#     -r ../../hive/plugins/posix_observer/posix_transport.ex \
#     -r ../../hive/plugins/posix_observer/posix_observer.ex \
#     -r ../../hive/plugins/posix_observer/posix_connector.ex \
#     -r ../../hive/plugins/k8s_observer/k8s_observer.ex \
#     ../../hive/plugins/k8s_observer/observer_test.exs

ExUnit.start(autorun: false)

defmodule Hive.K8s.ObserverTest do
  use ExUnit.Case, async: true

  alias Hive.K8s.Observer

  # The executor is a MODULE, like a POSIX transport, so the envelope can record its name.
  defmodule FakeClient do
    @behaviour Hive.K8s.Observer.Client
    @impl true
    def name, do: "fake-k8s"

    @impl true
    def read(read, opts) do
      case Map.fetch(Keyword.fetch!(opts, :bodies), read.resource) do
        {:ok, {:error, _} = err} -> err
        {:ok, body} -> {:ok, body}
        :error -> {:error, :forbidden}
      end
    end
  end

  defp with_bodies(bodies), do: [bodies: bodies]

  @pods %{
    "kind" => "PodList",
    "items" => [
      %{"metadata" => %{"namespace" => "prod", "name" => "api-0"}, "spec" => %{"nodeName" => "n1"}},
      # unscheduled: no node yet, so no placement fact -- inventing one would be the
      # inference this profile forbids
      %{"metadata" => %{"namespace" => "prod", "name" => "api-1"}, "spec" => %{}}
    ]
  }

  @deploys %{
    "kind" => "DeploymentList",
    "items" => [%{"metadata" => %{"namespace" => "prod", "name" => "api"}}]
  }

  describe "the default client refuses, on purpose" do
    test "observe/1 with no client configured cannot contact anything" do
      # The build machine holds a kubeconfig with cluster-admin credentials for real
      # production and for another client's infrastructure. A default that reached for it
      # would make "designed" and "ran against production" one forgotten argument apart.
      assert Observer.Client.Refusing.read(%{}, []) == {:error, :no_client_configured}

      o = Observer.observe(:pod_placement)
      # `partial`, NOT `unsupported`: we never asked, and recording our own omission as
      # "the cluster cannot answer" would be a claim about the cluster we have not earned.
      assert o.status == :partial
      assert o.detail =~ "nothing was contacted"
      refute o.status == :complete
    end
  end

  describe "reads, and the RBAC they become" do
    test "the namespace read is restricted to one named object, not the kind" do
      [ns_read] = Observer.reads(:cluster_identity)

      assert ns_read.verb == "get"
      assert ns_read.resource == "namespaces"
      assert ns_read.resource_names == ["kube-system"]
    end

    test "every read is a read — no write verb anywhere in the declaration" do
      verbs = Observer.allowlist() |> Enum.map(fn {_c, r} -> r.verb end) |> Enum.uniq()

      assert Enum.sort(verbs) == ["get", "list"]
      refute Enum.any?(verbs, &(&1 in ~w(create update patch delete deletecollection watch)))
    end

    test "no wildcards, and nothing reaching inside a container" do
      for {_class, r} <- Observer.allowlist() do
        refute r.resource == "*"
        refute r.api_group == "*"
        refute String.contains?(r.resource, "/")
        refute r.resource in ~w(secrets configmaps)
      end
    end
  end

  describe "observation" do
    test "pod_placement records where a pod runs, and says nothing about an unscheduled one" do
      o = Observer.observe(:pod_placement, FakeClient, with_bodies(%{"pods" => @pods}))

      assert o.status == :complete
      assert o.artifacts == [%{relation: "has_pod_on_node", object: "prod/api-0 on n1"}]
    end

    test "workloads names controllers, which are the continuants" do
      o =
        Observer.observe(
          :workloads,
          FakeClient,
          with_bodies(%{"deployments" => @deploys, "statefulsets" => %{"items" => []}, "daemonsets" => %{"items" => []}})
        )

      assert o.status == :complete
      assert o.artifacts == [%{relation: "has_workload", object: "prod/Deployment/api"}]
    end

    test "RBAC forbidding every read in a class is UNSUPPORTED — it closes nothing" do
      o = Observer.observe(:workloads, FakeClient, with_bodies(%{}))

      assert o.status == :unsupported
      assert o.detail =~ "forbidden"
    end

    test "one read failing among several is PARTIAL — it also closes nothing" do
      o =
        Observer.observe(
          :workloads,
          FakeClient,
          with_bodies(%{"deployments" => @deploys, "statefulsets" => {:error, :boom}, "daemonsets" => %{"items" => []}})
        )

      assert o.status == :partial
      assert o.artifacts == []
    end

    test "cluster identity is the kube-system uid" do
      o = Observer.observe(:cluster_identity, FakeClient, with_bodies(%{"namespaces" => %{"metadata" => %{"uid" => "u-1"}}}))

      assert Observer.identity_map(o) == %{cluster_uid: "u-1"}
    end
  end

  describe "the profile holds across two observers with different read types" do
    test "the same connector produces the same envelope for a cluster" do
      # ADR-22 question (c): a shell prober and an API prober share no read type -- argv
      # versus an RBAC-shaped map -- and were never meant to. What they share is the
      # ENVELOPE, which is what the profile was scoped to. This is that claim, tested.
      bodies = %{
        "namespaces" => %{"metadata" => %{"uid" => "u-1"}},
        "pods" => @pods,
        "deployments" => @deploys,
        "statefulsets" => %{"items" => []},
        "daemonsets" => %{"items" => []}
      }

      {:ok, page} =
        Hive.Posix.Connector.fetch(:start,
          observer: Observer,
          transport: FakeClient,
          bodies: bodies,
          targets: ["the-cluster"]
        )

      env = hd(page.events).envelope
      assert env.profile == "environment-observation/1"
      assert env.continuant_id == "env:k8s:u-1"
      assert env.continuity == :continuant
      assert env.intended_target == "the-cluster"
      assert env.coverage == %{environment: "env:k8s:u-1", class: :cluster_identity}
      assert Enum.map(page.events, & &1.envelope.observation_class) == Observer.classes()
      assert page.events |> Enum.map(& &1.envelope.snapshot_token) |> Enum.uniq() |> length() == 1
    end
  end
end

case ExUnit.run() do
  %{failures: 0} -> :ok
  _ -> System.halt(1)
end
