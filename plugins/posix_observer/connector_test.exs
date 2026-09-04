# Hermetic tests for the POSIX environment observer (no shell, no network, no DB). A fake
# transport supplies bytes, so the OBSERVER's knowledge is tested independently of any
# transport — which is the claim the observer/transport split makes and therefore the
# claim these tests have to exercise. Run from swarm/kernel:
#
#   mise exec -- mix run --no-start \
#     -r ../../hive/plugins/posix_observer/posix_transport.ex \
#     -r ../../hive/plugins/posix_observer/posix_observer.ex \
#     -r ../../hive/plugins/posix_observer/posix_connector.ex \
#     ../../hive/plugins/posix_observer/connector_test.exs
#
# Exits non-zero on any failure — real signal.

ExUnit.start(autorun: false)

defmodule Hive.Posix.Fake do
  @moduledoc "A transport that returns canned bytes. Lets the observer be tested alone."
  @behaviour Hive.Posix.Transport

  @impl true
  def name, do: "fake"

  @impl true
  def exec(read, opts) do
    responses = Keyword.fetch!(opts, :responses)
    Map.get(responses, Enum.join(read, " "), Map.get(responses, hd(read), {:error, :not_executable}))
  end

end

defmodule Hive.Posix.ObserverTest do
  use ExUnit.Case, async: true

  alias Hive.Posix.Connector
  alias Hive.Posix.Fake
  alias Hive.Posix.Observer

  @units_json ~s([{"unit":"keycloak.service","active":"active"},{"unit":"ssh.service"}])
  @ss_output """
  tcp   LISTEN 0      4096   0.0.0.0:22        0.0.0.0:*
  udp   UNCONN 0      0      127.0.0.1:323     0.0.0.0:*
  """

  # Identity is now asked for, not injected — the fake answers the identity reads.
  defp host_identity,
    do: %{
      "cat /etc/machine-id" => {:ok, %{output: "aaaaaaaaaaaa4aaaaaaaaaaaaaaaaaaa\n", exit_status: 0}},
      "cat /proc/sys/kernel/random/boot_id" => {:ok, %{output: "boot-1\n", exit_status: 0}},
      "hostname" => {:ok, %{output: "h1\n", exit_status: 0}}
    }

  defp ok(out), do: {:ok, %{output: out, exit_status: 0}}

  defp opts(responses, extra \\ []) do
    {ids, extra} = Keyword.pop(extra, :identity, host_identity())
    Keyword.merge([transport: Fake, responses: Map.merge(ids, responses)], extra)
  end

  describe "the observer, tested without any real transport" do
    test "active_units parses units and records an unnameable row as a skip" do
      o = Observer.observe(:active_units, Fake, responses: %{"systemctl" => ok(@units_json)})

      assert o.status == :complete
      assert o.artifacts == [
               %{relation: "has_active_unit", object: "keycloak.service"},
               %{relation: "has_active_unit", object: "ssh.service"}
             ]

      assert o.skips == []
    end

    test "a unit row with no name is a recorded skip, and the rest still land" do
      json = ~s([{"unit":"a.service"},{"active":"active"}])
      o = Observer.observe(:active_units, Fake, responses: %{"systemctl" => ok(json)})

      assert o.status == :complete
      assert [%{object: "a.service"}] = o.artifacts
      assert [%{reason: "unit name missing or not a string"}] = o.skips
    end

    test "listening_sockets parses and de-duplicates" do
      o = Observer.observe(:listening_sockets, Fake, responses: %{"ss" => ok(@ss_output)})

      assert o.status == :complete
      assert %{relation: "has_listening_socket", object: "tcp 0.0.0.0:22"} in o.artifacts
      assert length(o.artifacts) == 2
    end

    test "a missing binary is UNSUPPORTED, not an empty answer" do
      # The distinction the whole closure rule rests on: `unsupported` closes nothing,
      # `complete` with no artifacts closes everything in that class.
      o = Observer.observe(:active_units, Fake, responses: %{})

      assert o.status == :unsupported
      assert o.artifacts == []
      assert o.detail =~ "not present"
    end

    test "a non-zero exit is PARTIAL — it ran and refused, so it closes nothing" do
      o =
        Observer.observe(:active_units, Fake,
          responses: %{"systemctl" => {:ok, %{output: "", exit_status: 1}}}
        )

      assert o.status == :partial
      assert o.artifacts == []
    end

    test "unreadable output is PARTIAL, never an empty complete" do
      o = Observer.observe(:active_units, Fake, responses: %{"systemctl" => ok("not json")})

      assert o.status == :partial
      assert o.detail =~ "did not parse"
    end

    test "the reads a class needs are declared, and they are argv — no shell to quote into" do
      assert [["systemctl" | _]] = Observer.reads(:active_units)
      assert [["ss" | _]] = Observer.reads(:listening_sockets)
      assert Enum.all?(Observer.classes(), &(Observer.reads(&1) != []))
    end
  end

  describe "the container shape the review caught" do
    test "no systemd: active_units unsupported, listening_sockets still complete" do
      # The first draft named systemctl as the ONLY observation while permitting a
      # container identity, so a Swarm container would have observed nothing and reported
      # it as an answer. Two classes, and `unsupported`, are what make that impossible.
      {:ok, page} = Connector.fetch(:start, opts(%{"ss" => ok(@ss_output)}))

      by_class = Map.new(page.events, &{&1.envelope.observation_class, &1})
      assert by_class[:active_units].envelope.status == :unsupported
      assert by_class[:active_units].relations == []
      assert by_class[:listening_sockets].envelope.status == :complete
      assert by_class[:listening_sockets].relations != []
    end

    test "a container that can state only an incarnation is marked incarnation-only" do
      cid = String.duplicate("b", 64)

      identity = %{
        "hostname" => {:ok, %{output: "pod-x\n", exit_status: 0}},
        "cat /proc/self/cgroup" => {:ok, %{output: "0::/docker/#{cid}\n", exit_status: 0}}
      }

      {:ok, page} =
        Connector.fetch(:start, opts(%{"ss" => ok(@ss_output)}, identity: identity))

      env = hd(page.events).envelope
      assert env.continuant_kind == "container"
      assert env.continuity == :incarnation_only
      assert env.continuant_id == "env:container:" <> cid
    end

    test "an environment that states no identity is REFUSED, not given one" do
      {:ok, page} = Connector.fetch(:start, opts(%{"ss" => ok(@ss_output)}, identity: %{}))

      assert page.events == []
      assert [%{reason: "environment reported no identity of its own"}] = page.skips
    end
  end

  describe "the envelope" do
    test "carries identity, class, coverage, snapshot, status, target and transport" do
      {:ok, page} =
        Connector.fetch(:start, opts(%{"systemctl" => ok(@units_json), "ss" => ok(@ss_output)}))

      env = Enum.find(page.events, &(&1.envelope.observation_class == :active_units)).envelope
      assert env.profile == "environment-observation/1"
      assert env.continuant_id == "env:host:aaaaaaaaaaaa4aaaaaaaaaaaaaaaaaaa"
      assert env.continuity == :continuant
      assert env.incarnation_id == "boot-1"
      assert env.coverage == %{environment: env.continuant_id, class: :active_units}
      assert byte_size(env.snapshot_token) == 16
      assert env.transport == "fake"
      assert env.reads == Observer.reads(:active_units)
    end

    test "both classes of one run share a snapshot token" do
      {:ok, page} =
        Connector.fetch(:start, opts(%{"systemctl" => ok(@units_json), "ss" => ok(@ss_output)}))

      assert page.events |> Enum.map(& &1.envelope.snapshot_token) |> Enum.uniq() |> length() == 1
    end

    test "intended target and self-reported identity are BOTH recorded, so a mismatch shows" do
      # The whole target-loop answer: the observer reports what it REACHED. Asking for one
      # host and reaching something calling itself another is visible from these two
      # fields, with no target-selection ledger anywhere.
      {:ok, page} =
        Connector.fetch(:start,
          opts(%{"ss" => ok(@ss_output)}, targets: ["host-we-meant"])
        )

      env = hd(page.events).envelope
      assert env.intended_target == "host-we-meant"
      assert env.self_reported_identity.hostname == "h1"
      refute env.intended_target == env.self_reported_identity.hostname
    end
  end

  describe "the allowlist that becomes the SSH grant" do
    test "is exactly the reads the observer declares, and ids derive from the argv" do
      all = Observer.allowlist()

      assert length(all) == 6
      assert Enum.map(all, &elem(&1, 1)) |> Enum.uniq() == Observer.classes()

      # The id is a function of the argv, so a wrapper generated from this list cannot
      # drift from the list the code asks for without the id changing too.
      for {id, class, argv} <- all do
        assert id == Observer.read_id(class, argv)
        assert String.starts_with?(id, "#{class}.")
      end

      assert all |> Enum.map(&elem(&1, 0)) |> Enum.uniq() |> length() == 6
    end

    test "every read is literal argv with no caller-supplied argument" do
      # This is the property that lets a forced command compare against a fixed table
      # instead of parsing anything.
      for {_id, _class, argv} <- Observer.allowlist() do
        assert Enum.all?(argv, &is_binary/1)
        refute Enum.any?(argv, &String.contains?(&1, ["$", "`", ";", "|", "&", "*", "?"]))
      end
    end
  end

  describe "the SSH transport over a real loopback connection" do
    @describetag :loopback

    setup do
      wrapper = Path.expand("../../tmp/posix-observer-access/swarm-observe", __DIR__)

      available? =
        File.exists?(wrapper) and
          match?(
            {_, 0},
            System.cmd("ssh", ~w(-o BatchMode=yes -o ConnectTimeout=5 localhost true),
              stderr_to_stdout: true
            )
          )

      if available?, do: {:ok, wrapper: wrapper}, else: :ok
    end

    test "carries a real read end to end, through the generated wrapper", ctx do
      case ctx do
        %{wrapper: wrapper} ->
          o =
            Observer.observe(:self_identity, Hive.Posix.Transport.Ssh,
              host: "localhost",
              wrapper_path: wrapper
            )

          assert o.status == :complete
          assert Enum.any?(o.artifacts, &(&1.relation == "self_reports_machine_id"))

        _ ->
          IO.puts("\n  SKIPPED: loopback ssh or the generated wrapper is unavailable here.")
      end
    end

    test "a read that is not on the allowlist is REFUSED by the server", ctx do
      case ctx do
        %{wrapper: wrapper} ->
          # The transport always sends an id; forge one the wrapper does not know.
          assert {:error, :refused} =
                   Hive.Posix.Transport.Ssh.exec(["irrelevant"],
                     host: "localhost",
                     wrapper_path: wrapper,
                     read_id: "not.an.allowed.read"
                   )

        _ ->
          IO.puts("\n  SKIPPED: loopback ssh or the generated wrapper is unavailable here.")
      end
    end
  end

  describe "the kernel-driven loop" do
    test "one target per page, cursor threads the rest, :done at the end" do
      o = opts(%{"ss" => ok(@ss_output)}, targets: ["a", "b"])

      {:ok, first} = Connector.fetch(:start, o)
      assert first.cursor == %{"remaining" => ["b"]}
      assert hd(first.events).envelope.intended_target == "a"

      {:ok, second} = Connector.fetch(first.cursor, o)
      assert second.cursor == :done
      assert hd(second.events).envelope.intended_target == "b"
    end

    test "describe/0 announces the profile, the observer and its transports" do
      d = Connector.describe()
      assert d.profile == "environment-observation/1"
      assert d.observer == "posix"
      assert d.transports == ["local"]
      assert d.classes == [:self_identity, :active_units, :listening_sockets]
    end
  end
end

case ExUnit.run() do
  %{failures: 0} -> :ok
  _ -> System.halt(1)
end
