# Export corpus bodies containing a markdown table to /tmp/wiki_bodies.json for the host-side
# table parser. Intranet-private — stays in the container /tmp + host tmp (gitignored), never committed.
Logger.configure(level: :error)
alias Swarm.Repo

%{rows: rows} =
  Repo.query!(
    "SELECT n.id, n.key, c.body FROM content c JOIN node n ON n.id=c.node_id WHERE c.body ~ '\\|.*\\|.*\\|'"
  )

File.write!("/tmp/wiki_bodies.json", Jason.encode!(Enum.map(rows, fn [id, key, body] -> %{node_id: id, key: key, body: body} end)))
IO.puts("NETMAP-EXPORT #{length(rows)} table-bearing bodies")
