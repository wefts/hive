# Generate the SSH access artifact for the POSIX environment observer.
#
# The wrapper and the authorized_keys line are GENERATED FROM the observer's own
# declaration (`Hive.Posix.Observer.allowlist/0`), so what an operator grants is exactly
# what the code can ask for -- not a hand-written superset, and not a stale copy that
# drifted from the code months ago.
#
# Run from swarm/kernel:
#
#   SWARM_ENV=test mise exec -- mix run --no-start \
#     -r ../../hive/plugins/posix_observer/posix_transport.ex \
#     -r ../../hive/plugins/posix_observer/posix_observer.ex \
#     ../../hive/scripts/posix_observer_allowlist.exs [output-dir]
#
# Writes: swarm-observe (the wrapper), authorized_keys.line, README.md.
# Reads nothing, connects to nothing, requests nothing.

out_dir = List.first(System.argv()) || "../../hive/tmp/posix-observer-access"
File.mkdir_p!(out_dir)

allowlist = Hive.Posix.Observer.allowlist()

quoted = fn argv -> Enum.map_join(argv, " ", &inspect/1) end

cases =
  Enum.map_join(allowlist, "\n", fn {id, _class, argv} ->
    "    #{id})\n      exec #{Enum.map_join(argv, " ", &"'#{&1}'")}\n      ;;"
  end)

wrapper = """
#!/bin/sh
# swarm-observe -- the only command a Swarm observation key may run.
#
# GENERATED. Do not edit by hand: regenerate from the observer's declaration with
# hive/scripts/posix_observer_allowlist.exs, or the list here will drift from the list
# the software actually asks for.
#
# HOW IT IS REACHED
#   sshd runs this script INSTEAD OF whatever the client asked for, because the key is
#   restricted with command="..." in authorized_keys. Whatever the client sent is placed
#   in $SSH_ORIGINAL_COMMAND, and this script accepts it only if it is one of the exact
#   identifiers below. Anything else exits #{93} and runs nothing.
#
# WHAT IT PERMITS -- these #{length(allowlist)} reads and nothing else:
#{Enum.map_join(allowlist, "\n", fn {id, _c, argv} -> "#   #{id}\\n#       #{Enum.join(argv, " ")}" end)}
#
# WHAT IT REFUSES
#   Every other command. There is no fall-through, no shell, no argument passed from the
#   client into any command, and no way to compose one: the client sends an identifier,
#   never a command line, so there is nothing here to quote or escape.
#
# WHAT IT CANNOT DO
#   Write anything. Read anything not listed above. Open an interactive shell. Forward a
#   port. Forward an agent. Run as another user.

set -eu

req="${SSH_ORIGINAL_COMMAND:-${1:-}}"

case "$req" in
#{cases}
    *)
      echo "swarm-observe: refused (not an allowed read)" >&2
      exit 93
      ;;
esac
"""

authorized_keys_line = """
# Swarm environment observer -- read-only, #{length(allowlist)} permitted reads.
# Replace /usr/local/lib/swarm-observe with wherever you install the wrapper, and
# ssh-ed25519 AAAA...REPLACE_WITH_THE_PUBLIC_KEY with the key Swarm will present.
command="/usr/local/lib/swarm-observe",restrict ssh-ed25519 AAAA...REPLACE_WITH_THE_PUBLIC_KEY swarm-observer
"""

readme = """
# Granting Swarm read-only observation access to a host

You have been asked to let an automated system read a short, fixed list of facts from
this host. This describes exactly what it can do, and what it cannot. You do not need to
know anything about the system that is asking.

## What it wants to read

#{length(allowlist)} commands. That is the complete list -- not a summary of it.

| identifier the client may send | the command that runs |
| --- | --- |
#{Enum.map_join(allowlist, "\n", fn {id, _c, argv} -> "| `#{id}` | `#{Enum.join(argv, " ")}` |" end)}

All of them are read-only, none is interactive, none takes an argument supplied by the
client, and none produces output that depends on anything the client sends.

## How the restriction works

Two files:

1. **`swarm-observe`** -- a small shell script you install on this host, typically at
   `/usr/local/lib/swarm-observe`, owned by root and not writable by the account it runs
   as.
2. **one line in that account's `~/.ssh/authorized_keys`**, restricting the key so that
   sshd runs `swarm-observe` *instead of* whatever the client asks for.

The important property: **the restriction is enforced here, on this host, by sshd.** It
is not a setting in the client's configuration. A client that asks for something else
still gets `swarm-observe`, and `swarm-observe` refuses it and exits 93 without running
anything.

The client never sends a command line at all. It sends one of the identifiers in the
table above, and the wrapper looks it up. There is no string for anyone to inject into.

## What this access cannot do

- **No writes.** Nothing in the list modifies anything.
- **No other reads.** There is no fall-through case and no shell.
- **No interactive shell** -- `restrict` in the authorized_keys line disables pty
  allocation.
- **No port forwarding, no agent forwarding, no X11** -- also `restrict`.
- **No privilege.** It runs as whichever unprivileged account you put the key in. None of
  the listed reads needs root.

## Installing it

```sh
sudo install -o root -g root -m 0755 swarm-observe /usr/local/lib/swarm-observe
cat authorized_keys.line >> ~<the-account>/.ssh/authorized_keys   # then paste the real key
```

## Checking it yourself

Refusal, and the exit code that proves nothing ran:

```sh
SSH_ORIGINAL_COMMAND='rm -rf /' /usr/local/lib/swarm-observe; echo "exit=$?"   # exit=93
SSH_ORIGINAL_COMMAND='cat /etc/shadow' /usr/local/lib/swarm-observe; echo "exit=$?"  # exit=93
```

One permitted read, so you can see exactly what comes back:

```sh
SSH_ORIGINAL_COMMAND='#{allowlist |> List.first() |> elem(0)}' /usr/local/lib/swarm-observe
```

## Withdrawing it

Delete the line from `authorized_keys`. That is the whole revocation, and it is immediate.

## Provenance of this file

Generated from the observing software's own declaration of what it reads, so the list
above is the list it can ask for. If the software later wants a different read, this file
changes and you are asked again.
"""

File.write!(Path.join(out_dir, "swarm-observe"), wrapper)
File.chmod!(Path.join(out_dir, "swarm-observe"), 0o755)
File.write!(Path.join(out_dir, "authorized_keys.line"), authorized_keys_line)
File.write!(Path.join(out_dir, "README.md"), readme)

IO.puts("posix_observer_allowlist: #{length(allowlist)} reads")
for {id, _c, argv} <- allowlist, do: IO.puts("  #{id}  #{quoted.(argv)}")
IO.puts("wrote #{out_dir}/{swarm-observe,authorized_keys.line,README.md}")
