# EW-5 real validation on swarm_slice — does Scheduler→Worker→ML.Generation wire
# end-to-end against the live local model? PUBLIC scope only; AGGREGATE COUNTS ONLY
# (never prints claim content). Bounds the pass to ONE node. WIPE separately after.
#
#   SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 MIX_ENV=dev \
#     mise exec -- mix run --no-start ../../hive/scripts/ew_validate.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Enrichment.Scheduler
alias Swarm.Repo

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

# Bound the validation to ONE node (~120 s); the real local model from config.
cfg = Application.get_env(:swarm, :enrichment, [])
Application.put_env(:swarm, :enrichment, Keyword.put(cfg, :max_per_pass, 1))
IO.puts("model=#{cfg[:model]} max_per_pass=1 (validation)")

count = fn sql -> %{rows: [[n]]} = Repo.query!(sql); n end
n0 = count.("SELECT count(*) FROM node WHERE type <> 'article'")
e0 = count.("SELECT count(*) FROM edge WHERE evidence_kind = 'claim'")
IO.puts("BEFORE — non-article nodes: #{n0}, claim edges: #{e0}")

t0 = System.monotonic_time(:millisecond)
summary = Scheduler.run_pass([])
dt = div(System.monotonic_time(:millisecond) - t0, 1000)

IO.inspect(summary, label: "run_pass")
IO.puts("elapsed: #{dt}s")

ent = count.("SELECT count(*) FROM node WHERE type = 'entity'")
cl = count.("SELECT count(*) FROM edge WHERE evidence_kind = 'claim'")
wm = count.("SELECT count(*) FROM enrichment_watermark WHERE state = 'fresh'")
IO.puts("AFTER — entities minted: #{ent}, claim edges: #{cl}, fresh watermarks: #{wm}")
IO.puts(if(summary.enriched >= 1 and cl > 0, do: "RESULT: end-to-end OK", else: "RESULT: no claims written"))
