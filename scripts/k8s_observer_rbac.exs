# Generate the Kubernetes RBAC artifact for the control-plane observer.
#
# The ServiceAccount, ClusterRole and binding are GENERATED FROM the observer's own read
# declaration (`Hive.K8s.Observer.allowlist/0`), so what an administrator grants is
# exactly what the code can ask for -- not a hand-written superset, and not a copy that
# drifted from the code.
#
# Run from swarm/kernel:
#
#   SWARM_ENV=test mise exec -- mix run --no-start \
#     -r ../../hive/plugins/k8s_observer/k8s_observer.ex \
#     ../../hive/scripts/k8s_observer_rbac.exs [output-dir]
#
# Contacts nothing. Reads no kubeconfig. Requests no access.

out_dir = List.first(System.argv()) || "../../hive/tmp/k8s-observer-access"
File.mkdir_p!(out_dir)

allowlist = Hive.K8s.Observer.allowlist()

# One RBAC rule per (apiGroup, verb, resourceNames) group, resources merged. Splitting by
# resourceNames matters: a `get` on one named object must not be widened into a `get` on
# the kind just to keep the YAML short.
rules =
  allowlist
  |> Enum.map(&elem(&1, 1))
  |> Enum.group_by(&{&1.api_group, &1.verb, &1.resource_names})
  |> Enum.map(fn {{group, verb, names}, reads} ->
    %{
      api_group: group,
      verb: verb,
      resource_names: names,
      resources: reads |> Enum.map(& &1.resource) |> Enum.uniq() |> Enum.sort()
    }
  end)
  |> Enum.sort_by(&{&1.api_group, &1.verb})

yaml_list = fn items -> Enum.map_join(items, ", ", &~s("#{&1}")) end

rules_yaml =
  Enum.map_join(rules, "\n", fn r ->
    names =
      if r.resource_names == [],
        do: "",
        else: "\n    resourceNames: [#{yaml_list.(r.resource_names)}]"

    """
      - apiGroups: [#{yaml_list.([r.api_group])}]
        resources: [#{yaml_list.(r.resources)}]
        verbs: [#{yaml_list.([r.verb])}]#{String.replace(names, "\n    ", "\n    ")}\
    """
  end)

manifest = """
# Swarm environment observer -- read-only Kubernetes access.
#
# GENERATED from the observer's own read declaration. Do not edit by hand: regenerate with
# hive/scripts/k8s_observer_rbac.exs, or this will drift from what the software asks for.
#
# This grants #{length(allowlist)} reads and nothing else. There is no write verb here, no
# `watch`, no `exec`, no access to Secrets, and no wildcard.
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: swarm-observer
  namespace: swarm-observer
---
apiVersion: v1
kind: Namespace
metadata:
  name: swarm-observer
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: swarm-observer-read
rules:
#{rules_yaml}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: swarm-observer-read
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: swarm-observer-read
subjects:
  - kind: ServiceAccount
    name: swarm-observer
    namespace: swarm-observer
"""

readme = """
# Granting Swarm read-only observation access to a Kubernetes cluster

You have been asked to let an automated system read a short, fixed list of things from
this cluster. This says exactly what it can read and what it cannot. You do not need to
know anything about the system that is asking.

## What it reads

#{length(allowlist)} reads. That is the complete list, not a summary.

| observation | verb | apiGroup | resource | limited to |
| --- | --- | --- | --- | --- |
#{Enum.map_join(allowlist, "\n", fn {class, r} -> "| #{class} | `#{r.verb}` | `#{if r.api_group == "", do: "core", else: r.api_group}` | `#{r.resource}` | #{if r.resource_names == [], do: "—", else: Enum.join(r.resource_names, ", ")} |" end)}

What that adds up to, in words: **which workloads exist, which node each pod is running
on, and the cluster's own UID so observations can be attributed to this cluster and not
another.**

## What it cannot do

- **No writes of any kind.** There is no `create`, `update`, `patch` or `delete` in the
  role. Applying it cannot change anything in the cluster.
- **No Secrets, ConfigMaps, or object contents beyond metadata and pod placement.**
- **No `exec`, no `attach`, no `portforward`, no `proxy`** — nothing that reaches inside a
  running container.
- **No `watch`** — it reads on a schedule and holds no open stream.
- **No wildcards.** Every rule names its resources explicitly, and the namespace read is
  restricted with `resourceNames` to `kube-system` alone.

## Applying it

```sh
kubectl apply -f swarm-observer-rbac.yaml
```

Then issue a token for the ServiceAccount and give it to whoever asked:

```sh
kubectl -n swarm-observer create token swarm-observer --duration=24h
```

Prefer a short duration and a renewal over a long-lived token.

## Checking it yourself

`kubectl auth can-i` answers as the ServiceAccount, so you can confirm both what it can do
and what it cannot:

```sh
SA=system:serviceaccount:swarm-observer:swarm-observer
kubectl auth can-i list pods --as=$SA                 # yes
kubectl auth can-i get namespace/kube-system --as=$SA # yes
kubectl auth can-i list secrets --as=$SA              # no
kubectl auth can-i create pods --as=$SA               # no
kubectl auth can-i create pods/exec --as=$SA          # no
kubectl auth can-i delete deployments --as=$SA        # no
```

## Withdrawing it

```sh
kubectl delete -f swarm-observer-rbac.yaml
```

That removes the account, the role and the binding together, and it is immediate.

## Why a dedicated account and not an existing admin credential

Because an admin credential would grant everything on this list and everything else
besides, and because an action taken with a person's credential is attributable to that
person. A scoped ServiceAccount is auditable as itself. The same reasoning produced a
dedicated read-only account for the hypervisor API rather than reusing an operator login.

## Provenance of this file

Generated from the observing software's own declaration of the reads it makes. If it later
needs a different read, this file changes and you are asked again.
"""

File.write!(Path.join(out_dir, "swarm-observer-rbac.yaml"), manifest)
File.write!(Path.join(out_dir, "README.md"), readme)

IO.puts("k8s_observer_rbac: #{length(allowlist)} reads -> #{length(rules)} RBAC rules")

for {class, r} <- allowlist do
  names = if r.resource_names == [], do: "", else: " resourceNames=#{inspect(r.resource_names)}"
  IO.puts("  #{class}: #{r.verb} #{if r.api_group == "", do: "core", else: r.api_group}/#{r.resource}#{names}")
end

IO.puts("wrote #{out_dir}/{swarm-observer-rbac.yaml,README.md}")
