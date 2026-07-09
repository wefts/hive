# Purge the entire who-is-who substrate (all who: entity nodes → CASCADE drops their edges +
# profile content). The `concept:who:kind:*` type markers are shared/stable and kept. Idempotent.
# Run: docker exec hive-kernel-1 /app/bin/swarm rpc "$(cat purge_who.exs)"
Logger.configure(level: :error)
alias Swarm.Repo

%{num_rows: dropped} =
  Repo.query!(
    "DELETE FROM node WHERE key LIKE 'who:person:%' OR key LIKE 'who:team:%' " <>
      "OR key LIKE 'who:role:%' OR key LIKE 'who:site:%'"
  )

IO.puts("WHO-PURGE removed_nodes=#{dropped} (edges + content cascaded)")
