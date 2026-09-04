defmodule Hive.Posix.Connector do
  @moduledoc """
  The environment-observation connector, v1: **POSIX observer, local transport, explicit
  targets from configuration** (workspace ADR-22). Conforms to the environment-observation
  profile on the `connector` kind (`docs/architecture/ports.md`).

  One new thing at a time. Graph-supplied targets and the Kubernetes control-plane observer
  come later and separately — introducing a new observer, a new transport and a new target
  source together makes a failure unattributable to any of them, which is the lesson the
  placement ablation paid for.

  ## What this emits, stated so no reader mistakes it for more

  **A history of literal runtime artifacts on one environment incarnation.** Not service
  placement. `env:<id> has_active_unit "keycloak.service"` does not say Keycloak runs
  there, and `env:<id>` is not tied to any inventory host. Both identity edges the
  document→host join needs are still missing, and this connector closes neither.

  ## The run envelope

  Every page carries an envelope, because none of the profile's rules are expressible
  without one — continuant and incarnation identity, the class, the coverage boundary, a
  snapshot token, per-class `complete`/`partial`/`unsupported`, the intended target, the
  environment's **self-reported** identity, and the transport.

  Absence may close a fact **only** for that environment, that class, after a completed
  final page. Not across classes, not on `partial`, and not on `unsupported`.

  ## Identity

  Facts attach to the **continuant** — the thing that persists across restarts. The
  **incarnation** is recorded per run, so a redeployment reads as a new incarnation of the
  same subject rather than a new subject.

  An environment that can self-report only an incarnation (a typical container) is recorded
  as **incarnation-only**: its facts are explicitly non-continuous, do not survive
  redeployment, and claim no history across incarnations. It is never handed a continuant
  identity from configuration — that would be the observer reporting what it was told
  rather than what it found.
  """

  @behaviour Swarm.Ports.Connector

  alias Hive.Posix.Observer
  alias Hive.Posix.Transport

  require Logger

  @source "environment"
  @profile "environment-observation/1"

  @impl true
  def describe,
    do: %{
      name: "posix_observer",
      kind: :connector,
      profile: @profile,
      source: @source,
      sync_modes: [:full],
      observer: "posix",
      transports: ["local"],
      classes: Observer.classes()
    }

  @impl true
  def fetch(:start, opts), do: fetch_targets(targets(opts), opts)
  def fetch(%{"remaining" => targets}, opts), do: fetch_targets(targets, opts)
  def fetch(other, _opts), do: {:error, {:bad_cursor, other}}

  # One target per page: the kernel drives the loop, and a target that fails fails alone.
  defp fetch_targets([], _opts), do: {:ok, %{events: [], skips: [], cursor: :done, truncated?: false}}

  defp fetch_targets([target | rest], opts) do
    transport = transport_for(target, opts)
    observer = Keyword.get(opts, :observer, Observer)

    # Identity is the first observation class, asked through the transport like any other.
    # The transport has no identity knowledge of its own -- see the note in Transport.
    [identity_class | _] = observer.classes()
    id_obs = observer.observe(identity_class, transport, opts)
    identity = observer.identity_map(id_obs)

    case continuant(identity) do
      :none ->
        # It could not say anything about itself. Refuse rather than invent one.
        {:ok,
         %{
           events: [],
           skips: [
             %{
               source_ref: "env:unidentified",
               reason: "environment reported no identity of its own",
               occurred_at: now(opts)
             }
           ],
           cursor: cursor(rest),
           truncated?: false
         }}

      {kind, key, continuity} ->
        observed = now(opts)
        snapshot = snapshot_token(observed, key)

        {events, skips} =
          observer.classes()
          |> Enum.map(fn
            ^identity_class -> id_obs
            class -> observer.observe(class, transport, opts)
          end)
          |> Enum.map_reduce([], fn obs, acc ->
            {event(obs, kind, key, continuity, identity, target, transport, observed, snapshot),
             acc ++ class_skips(obs, key, observed)}
          end)

        {:ok, %{events: events, skips: skips, cursor: cursor(rest), truncated?: false}}
    end
  end

  defp cursor([]), do: :done
  defp cursor(rest), do: %{"remaining" => rest}

  # --- identity ------------------------------------------------------------------------

  # The continuant is what facts attach to. A machine-id denotes an OS installation and
  # survives restarts; a container id denotes an incarnation and does not. Returning the
  # continuity level explicitly stops the caller quietly treating one as the other.
  defp continuant(%{machine_id: id}) when is_binary(id) and id != "",
    do: {"host", "env:host:#{id}", :continuant}

  defp continuant(%{container_id: id}) when is_binary(id) and id != "",
    do: {"container", "env:container:#{id}", :incarnation_only}

  # A hostname is not an identity -- it is renameable and not unique. An environment that
  # can offer only a hostname is incarnation-only, and says so.
  defp continuant(%{hostname: h}) when is_binary(h) and h != "",
    do: {"unidentified", "env:hostname:#{h}", :incarnation_only}

  # A cluster's continuant is the kube-system namespace uid: it survives control-plane
  # restarts and node churn, which is what a continuant has to do.
  defp continuant(%{cluster_uid: u}) when is_binary(u) and u != "",
    do: {"cluster", "env:k8s:#{u}", :continuant}

  defp continuant(_), do: :none

  defp incarnation(%{boot_id: b}) when is_binary(b) and b != "", do: b
  defp incarnation(%{container_id: c}) when is_binary(c) and c != "", do: c
  defp incarnation(_), do: nil

  # --- events --------------------------------------------------------------------------

  defp event(obs, kind, key, continuity, identity, target, transport, observed, snapshot) do
    ref = "#{@source}:#{key}:#{obs.class}"

    %{
      provenance: "#{ref}@#{DateTime.to_iso8601(observed)}",
      origin: ref,
      source: @source,
      source_ref: ref,
      occurred_at: observed,
      valid_time: observed,
      envelope: %{
        profile: @profile,
        continuant_id: key,
        continuant_kind: kind,
        continuity: continuity,
        incarnation_id: incarnation(identity),
        observation_class: obs.class,
        coverage: %{environment: key, class: obs.class},
        snapshot_token: snapshot,
        status: obs.status,
        # What was asked for, and what actually answered. The mismatch between them is
        # computable from these two fields alone -- no target-selection ledger needed.
        intended_target: target,
        self_reported_identity: identity,
        transport: transport.name(),
        reads: obs.reads,
        detail: obs.detail
      },
      entities: [%{type: "entity", key: key, identity: key, scope: env_scope(), content: ""}],
      relations:
        Enum.map(obs.artifacts, fn a ->
          %{from: key, from_ref: key, to: a.object, to_ref: nil, type: a.relation}
        end)
    }
  end

  defp class_skips(%{skips: []}, _key, _observed), do: []

  defp class_skips(%{skips: skips, class: class}, key, observed) do
    Enum.map(skips, fn s ->
      %{source_ref: "#{@source}:#{key}:#{class}", reason: s.reason, occurred_at: observed}
    end)
  end

  # --- configuration --------------------------------------------------------------------

  # v1 targets are an EXPLICIT list in configuration. No graph, no discovery.
  defp targets(opts) do
    case Keyword.get(opts, :targets) do
      [_ | _] = list -> list
      _ -> ["self"]
    end
  end

  defp transport_for(_target, opts),
    do: Keyword.get(opts, :transport, Transport.Local)

  defp env_scope, do: System.get_env("SWARM_ENV_SCOPE", "private")

  defp now(opts) do
    case Keyword.get(opts, :clock) do
      f when is_function(f, 0) -> f.()
      _ -> DateTime.utc_now()
    end
  end

  # One token per run per environment, so pages of one logical read are known to belong to
  # the same snapshot. v1 is single-page per class; the field exists so a paginating
  # transport does not have to change the contract.
  defp snapshot_token(observed, key),
    do:
      :crypto.hash(:sha256, "#{key}|#{DateTime.to_iso8601(observed)}")
      |> Base.encode16(case: :lower)
      |> binary_part(0, 16)
end
