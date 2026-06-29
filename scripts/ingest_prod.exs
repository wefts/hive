# Fuller prod ingest — Confluence + MediaWiki → swarm_prod, embedded via the real
# bge-m3 boundary. Adapted from conn_2source_slice.exs for the post-ML-fix world:
# starts Swarm.ML.ChannelPool (the long-lived pooled boundary) instead of the old
# per-call GRPC.Client.Supervisor, embeds concurrently across the pool, and reports
# PRIVACY-SAFE metrics only (counts/timings — never titles/prose/URLs).
#
#   set -a; . hive/secrets.env; set +a
#   cd swarm/kernel && SWARM_DB_NAME=swarm_prod SWARM_ML_ADDRESS=172.19.0.5:50051 \
#     MIX_ENV=dev mise exec -- mix run --no-start \
#     -r ../../hive/plugins/confluence_connector/confluence_connector.ex \
#     -r ../../hive/plugins/mediawiki_connector/mediawiki_connector.ex \
#     ../../hive/scripts/ingest_prod.exs
#
# Tunables (generous defaults for a real corpus, bounded so it completes):
#   CONF_MAXPAGES (30) CONF_LIMIT (50) WIKI_MAXPAGES (30) WIKI_GAPLIMIT (50) EMBED_CONC (4)

require Logger
Logger.configure(level: :warning)

alias Swarm.Connector.Sync
alias Swarm.Ingest.Content
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Application.ensure_all_started(:inets)
{:ok, _} = Application.ensure_all_started(:ssl)
{:ok, _} = Repo.start_link()
{:ok, _} = Swarm.Ingest.Dedup.start_link([])
# grpc 0.11.5's GRPC.Client.Connection registers its conns under this supervisor —
# the pool's workers connect through it, so it must be running (the app tree starts
# it normally; a --no-start script starts it manually).
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)
# The pooled ML boundary (long-lived channels; never the per-call disconnect that
# crashes grpc 0.11.5). Workers connect to SWARM_ML_ADDRESS.
{:ok, _} = Swarm.ML.ChannelPool.start_link([])

env = fn k, d -> String.to_integer(System.get_env(k, d)) end
count = fn sql -> %{rows: [[n]]} = Repo.query!(sql); n end

report = fn label, {:ok, r}, ms ->
  IO.puts("  #{label}: ingested=#{r.ingested} dup=#{r.duplicates} err=#{r.errors} " <>
            "pages=#{r.pages} complete?=#{r.complete?} in #{ms} ms")
  r
end

IO.puts("== prod ingest (db=#{System.get_env("SWARM_DB_NAME", "?")} ml=#{System.get_env("SWARM_ML_ADDRESS", "?")}) ==")
IO.puts("before: nodes=#{count.("SELECT count(*) FROM node")}")

# --- 1. ingest both intranet sources (scope=group → privacy-gated content) ---
IO.puts("\n-- ingest (kernel-driven Sync loop) --")

cookie =
  case Hive.MediaWiki.Connector.login([]) do
    {:ok, c} -> IO.puts("  mediawiki login: OK"); c
    {:error, r} -> IO.puts("  mediawiki login: anon (#{inspect(r)})"); nil
  end

t = System.monotonic_time(:millisecond)
conf =
  report.(
    "confluence",
    Sync.run(Hive.Confluence.Connector,
      scope: "group",
      limit: env.("CONF_LIMIT", "50"),
      max_pages: env.("CONF_MAXPAGES", "30")
    ),
    System.monotonic_time(:millisecond) - t
  )

t = System.monotonic_time(:millisecond)
wiki =
  report.(
    "mediawiki",
    Sync.run(Hive.MediaWiki.Connector,
      scope: "group",
      cookie: cookie,
      gaplimit: env.("WIKI_GAPLIMIT", "50"),
      max_pages: env.("WIKI_MAXPAGES", "30")
    ),
    System.monotonic_time(:millisecond) - t
  )

# --- 2. embed newly-ingested content (real bge-m3), concurrent across the pool ---
%{rows: pending} =
  Repo.query!(
    "SELECT c.node_id FROM content c WHERE NOT EXISTS (SELECT 1 FROM chunk k WHERE k.node_id = c.node_id)"
  )

total = length(pending)
conc = env.("EMBED_CONC", "4")
IO.puts("\n-- embed (real bge-m3): #{total} pending, concurrency #{conc} --")
t = System.monotonic_time(:millisecond)

{ok, failed} =
  pending
  |> Stream.with_index(1)
  |> Task.async_stream(
    fn {[id], i} ->
      res = Content.embed(id, [])
      if rem(i, 50) == 0, do: IO.puts("  ...#{i}/#{total}")
      res
    end,
    max_concurrency: conc,
    timeout: 120_000,
    on_timeout: :kill_task
  )
  |> Enum.reduce({0, 0}, fn
    {:ok, {:ok, _}}, {o, f} -> {o + 1, f}
    _, {o, f} -> {o, f + 1}
  end)

t_embed = System.monotonic_time(:millisecond) - t
IO.puts("  embedded ok=#{ok} failed=#{failed} in #{t_embed} ms")

# --- 3. population (privacy-safe: counts only) ---
IO.puts("\n-- population --")
IO.puts("  nodes: #{count.("SELECT count(*) FROM node")} " <>
          "(public=#{count.("SELECT count(*) FROM node WHERE scope='public'")} " <>
          "group=#{count.("SELECT count(*) FROM node WHERE scope='group'")})")
IO.puts("  edges=#{count.("SELECT count(*) FROM edge")} " <>
          "content=#{count.("SELECT count(*) FROM content")} " <>
          "chunks=#{count.("SELECT count(*) FROM chunk")} " <>
          "vec=#{count.("SELECT count(*) FROM node WHERE vec IS NOT NULL")}")

IO.puts("\nRESULT: prod corpus ingested+embedded (privacy-safe counts above). " <>
          "conf complete?=#{conf.complete?} wiki complete?=#{wiki.complete?}")
