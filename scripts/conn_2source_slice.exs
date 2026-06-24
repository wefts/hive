# Campaign A / A3 — multi-source live slice + re-measure. Ingests the real intranet
# Confluence + MediaWiki through the ADR-5 Sync loop into an ISOLATED DB (alongside
# the existing public Wikipedia slice → a 3-source, mixed-scope graph), embeds via
# the real bge-m3 boundary, then reports PRIVACY-SAFE metrics only (counts, timings,
# coverage, fragmentation group COUNTS, traversal cost) — never titles/prose/URLs.
#
#   set -a; . hive/secrets.env; set +a
#   cd swarm/kernel && SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 \
#     MIX_ENV=dev mise exec -- mix run --no-start \
#     -r ../../hive/plugins/confluence_connector/confluence_connector.ex \
#     -r ../../hive/plugins/mediawiki_connector/mediawiki_connector.ex \
#     ../../hive/scripts/conn_2source_slice.exs
#
# Tunables: CONF_MAXPAGES (4), CONF_LIMIT (50), WIKI_MAXPAGES (7), WIKI_GAPLIMIT (30).

require Logger
Logger.configure(level: :warning)

alias Swarm.Connector.Sync
alias Swarm.Graph.Traverse
alias Swarm.Ingest.Content
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Application.ensure_all_started(:inets)
{:ok, _} = Application.ensure_all_started(:ssl)
{:ok, _} = Repo.start_link()
{:ok, _} = Swarm.Ingest.Dedup.start_link([])
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

env = fn k, d -> String.to_integer(System.get_env(k, d)) end
count = fn sql -> %{rows: [[n]]} = Repo.query!(sql); n end

report = fn label, {:ok, r}, ms ->
  IO.puts("  #{label}: ingested=#{r.ingested} dup=#{r.duplicates} err=#{r.errors} " <>
    "pages=#{r.pages} ceilings=#{r.ceilings} complete?=#{r.complete?} in #{ms} ms")
  r
end

IO.puts("== A3 multi-source slice (db=#{System.get_env("SWARM_DB_NAME", "?")} ml=#{System.get_env("SWARM_ML_ADDRESS", "?")}) ==")
IO.puts("before: nodes=#{count.("SELECT count(*) FROM node")} " <>
  "(public=#{count.("SELECT count(*) FROM node WHERE scope='public'")} " <>
  "group=#{count.("SELECT count(*) FROM node WHERE scope='group'")})")

# --- 1. ingest both intranet sources ---
IO.puts("\n-- ingest (kernel-driven Sync loop) --")

cookie =
  case Hive.MediaWiki.Connector.login([]) do
    {:ok, c} -> IO.puts("  mediawiki login: OK"); c
    {:error, r} -> IO.puts("  mediawiki login: anon (#{inspect(r)})"); nil
  end

t = System.monotonic_time(:millisecond)
conf =
  report.("confluence",
    Sync.run(Hive.Confluence.Connector, scope: "group", limit: env.("CONF_LIMIT", "50"), max_pages: env.("CONF_MAXPAGES", "4")),
    System.monotonic_time(:millisecond) - t)

t = System.monotonic_time(:millisecond)
wiki =
  report.("mediawiki",
    Sync.run(Hive.MediaWiki.Connector, scope: "group", cookie: cookie, gaplimit: env.("WIKI_GAPLIMIT", "30"), max_pages: env.("WIKI_MAXPAGES", "7")),
    System.monotonic_time(:millisecond) - t)

# --- 2. embed newly-ingested content (real bge-m3) ---
%{rows: pending} =
  Repo.query!("SELECT c.node_id FROM content c WHERE NOT EXISTS (SELECT 1 FROM chunk k WHERE k.node_id = c.node_id)")

IO.puts("\n-- embed (real bge-m3): #{length(pending)} pending --")
t = System.monotonic_time(:millisecond)
{ok, failed} =
  Enum.reduce(pending, {0, 0}, fn [id], {o, f} ->
    case Content.embed(id, []) do
      {:ok, _} -> {o + 1, f}
      {:error, _} -> {o, f + 1}
    end
  end)
t_embed = System.monotonic_time(:millisecond) - t
IO.puts("  embedded ok=#{ok} failed=#{failed} in #{t_embed} ms")

# --- 3. graph population (multi-source, mixed-scope) ---
IO.puts("\n-- population --")
IO.puts("  nodes total: #{count.("SELECT count(*) FROM node")}")
IO.puts("  by scope: public=#{count.("SELECT count(*) FROM node WHERE scope='public'")} " <>
  "group=#{count.("SELECT count(*) FROM node WHERE scope='group'")} " <>
  "private=#{count.("SELECT count(*) FROM node WHERE scope='private'")}")
IO.puts("  edges: #{count.("SELECT count(*) FROM edge")}  content rows: #{count.("SELECT count(*) FROM content")}  chunks: #{count.("SELECT count(*) FROM chunk")}")
IO.puts("  group-scope nodes with vec: #{count.("SELECT count(*) FROM node WHERE scope='group' AND vec IS NOT NULL")}")

# --- 4. fragmentation probe (PRIVACY-SAFE: group COUNT + sizes only, no keys) ---
%{rows: frag} =
  Repo.query!("""
  SELECT count(*) AS groups, COALESCE(sum(n),0) AS colliding FROM (
    SELECT lower(key) f, count(*) n FROM node GROUP BY type, lower(key) HAVING count(*) > 1
  ) s
  """)

[[frag_groups, frag_nodes]] = frag
IO.puts("\n-- fragmentation probe (entity-resolution) --")
IO.puts("  case-folded collision groups: #{frag_groups}  (#{frag_nodes} nodes involved)")

# --- 5. traversal cost on the densest nodes (council caveat: ADR-3 recursive-CTE
#        path enumeration is worst-case exponential in branching × depth). Sample
#        the top hubs and push to depth 4 to expose any compounding on the denser
#        cross-linked org graph, not just a shallow star. ---
%{rows: hubs} =
  Repo.query!("SELECT src, count(*) d FROM edge GROUP BY src ORDER BY d DESC LIMIT 3")

IO.puts("\n-- traversal cost (top-3 hubs; council ADR-3 caveat) --")
for [hub_id, deg] <- hubs do
  costs =
    for depth <- [2, 3, 4] do
      t = System.monotonic_time(:microsecond)
      hits = Traverse.traverse(hub_id, depth)
      {depth, length(hits), Float.round((System.monotonic_time(:microsecond) - t) / 1000, 2)}
    end

  summary = Enum.map_join(costs, "  ", fn {d, r, ms} -> "d#{d}:#{r}n/#{ms}ms" end)
  IO.puts("  hub(out=#{deg}): #{summary}")
end

IO.puts("\nRESULT: 2 intranet sources ingested+embedded; metrics above (privacy-safe). " <>
  "conf complete?=#{conf.complete?} wiki complete?=#{wiki.complete?}")
