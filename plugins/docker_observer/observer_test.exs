# Hermetic tests for the Docker orchestrator observer. No daemon is contacted.
#
#   SWARM_ENV=test mise exec -- mix run --no-start \
#     -r ../../hive/plugins/posix_observer/posix_transport.ex \
#     -r ../../hive/plugins/posix_observer/posix_observer.ex \
#     -r ../../hive/plugins/posix_observer/posix_connector.ex \
#     -r ../../hive/plugins/docker_observer/docker_observer.ex \
#     ../../hive/plugins/docker_observer/observer_test.exs

ExUnit.start(autorun: false)

defmodule Hive.Docker.Fake do
  @behaviour Hive.Posix.Transport
  @impl true
  def name, do: "fake"
  @impl true
  def exec(argv, opts) do
    Map.get(Keyword.fetch!(opts, :responses), Enum.at(argv, 1), {:error, :not_executable})
  end
end

defmodule Hive.Docker.ObserverTest do
  use ExUnit.Case, async: true

  alias Hive.Docker.Fake
  alias Hive.Docker.Observer

  defp ok(out), do: {:ok, %{output: out, exit_status: 0}}

  @labels "com.docker.compose.project=hive,com.docker.compose.service=kernel,other=x"
  @ps ~s({"ID":"7a76fc60ca81","Names":"hive-kernel-1","Labels":"#{@labels}"}\n) <>
        ~s({"ID":"deadbeef0000","Names":"loose","Labels":""}\n)

  defp opts, do: [responses: %{"system" => ok("daemon-uuid\n"), "ps" => ok(@ps)}]

  test "the bridge: it states which service a container id belongs to" do
    # The in-container POSIX observer can report its hostname, which IS the container id,
    # and nothing else durable. This observer states the other half. Neither infers the
    # link; they meet on the same literal string.
    o = Observer.observe(:containers, Fake, opts())

    assert o.status == :complete
    assert %{relation: "has_container", object: "7a76fc60ca81"} in o.artifacts
    assert %{relation: "container_of_service", object: "7a76fc60ca81 in hive/kernel"} in o.artifacts
  end

  test "a container with no compose labels gets its id and no invented service" do
    o = Observer.observe(:containers, Fake, opts())

    assert %{relation: "has_container", object: "deadbeef0000"} in o.artifacts
    refute Enum.any?(o.artifacts, &(&1.relation == "container_of_service" and &1.object =~ "deadbeef"))
  end

  test "an unparseable line is a recorded skip, never a silent drop" do
    o = Observer.observe(:containers, Fake, responses: %{"ps" => ok("not json\n" <> @ps)})

    assert length(o.skips) == 1
    assert length(o.artifacts) == 3
  end

  test "no docker binary is UNSUPPORTED, which closes nothing" do
    o = Observer.observe(:containers, Fake, responses: %{})

    assert o.status == :unsupported
  end

  test "the daemon id becomes a continuant through the shared connector" do
    {:ok, page} =
      Hive.Posix.Connector.fetch(:start,
        [observer: Observer, transport: Fake, targets: ["local-dockerd"]] ++ opts()
      )

    env = hd(page.events).envelope
    assert env.continuant_id == "env:dockerd:daemon-uuid"
    assert env.continuity == :continuant
    assert env.profile == "environment-observation/1"
    assert Enum.map(page.events, & &1.envelope.observation_class) == Observer.classes()
  end

  test "every read is a read — docker subcommands only, no run/exec/rm" do
    for {_class, read} <- Observer.allowlist() do
      assert hd(read) == "docker"
      assert Enum.at(read, 1) in ~w(system ps)
      refute Enum.any?(read, &(&1 in ~w(run exec rm kill stop start build push)))
    end
  end
end

case ExUnit.run() do
  %{failures: 0} -> :ok
  _ -> System.halt(1)
end
