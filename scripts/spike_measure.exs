# Cognitive-activation spike — MEASURE the enriched graph (runs after spike_activate.exs).
# Three observations the card asks for, aggregate numbers only (privacy-safe):
#  (A) Corroboration / ADR-9 stress: claim-edge seen_count distribution (distinct-source
#      reinforcement) + what fraction is multi-source — the over-corroboration surface.
#  (B) node.vec CONSUMER (D5, first real reader): embed every spike entity name (bge-m3),
#      set node.vec, ANN each against the others; count near-duplicate pairs the exact-key
#      upsert MISSED — soft entity-resolution candidates (feeds entity-resolution + node-vec-per-type).
#  (C) Traversal cost on the now-denser graph (ADR-3 wall trigger) vs the ~2ms article baseline.
#
#   SIM=0.86 SWARM_DB_NAME=swarm_slice SWARM_ML_ADDRESS=172.19.0.5:50051 \
#     MIX_ENV=dev mise exec -- mix run --no-start ../../hive/scripts/spike_measure.exs

require Logger
Logger.configure(level: :warning)
alias Swarm.Repo
alias Swarm.Graph.Traverse

{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Application.ensure_all_started(:grpc)
{:ok, _} = Repo.start_link()
{:ok, _} = DynamicSupervisor.start_link(strategy: :one_for_one, name: GRPC.Client.Supervisor)

alias Swarm.ML.Embeddings
sim = String.to_float(System.get_env("SIM", "0.86"))
q = fn sql, p -> Repo.query!(sql, p).rows end

IO.puts("== SPIKE MEASURE ==")

# Spike rows = non-article nodes + claim edges (relation not in structural set).
[[ent]] = q.("SELECT count(*) FROM node WHERE type<>'article'", [])
[[claim_edges]] = q.("SELECT count(*) FROM edge WHERE type NOT IN ('links_to','child_of')", [])
IO.puts("entities=#{ent}  claim_edges=#{claim_edges}")

# ── (A) corroboration / ADR-9 stress ────────────────────────────────────────
hist = q.("SELECT seen_count, count(*) FROM edge WHERE type NOT IN ('links_to','child_of') GROUP BY seen_count ORDER BY 1", [])
[[multi]] = q.("SELECT count(*) FROM edge WHERE type NOT IN ('links_to','child_of') AND seen_count>=2", [])
[[maxsc]] = q.("SELECT coalesce(max(seen_count),0) FROM edge WHERE type NOT IN ('links_to','child_of')", [])
IO.puts("\n(A) corroboration — claim-edge seen_count (distinct-source reinforcement):")
Enum.each(hist, fn [sc, c] -> IO.puts("    seen_count=#{sc}: #{c} edges") end)
IO.puts("    multi-source (>=2): #{multi}/#{claim_edges}  max seen_count=#{maxsc}")
IO.puts("    NB: live Traverse ignores seen_count (uses reliability·decay only) — combine_typed unwired.")

# ── (B) node.vec consumer: embed spike entities, ANN for near-dup pairs ──────
ent_rows = q.("SELECT id, key FROM node WHERE type<>'article' ORDER BY id", [])
IO.puts("\n(B) node.vec consumer — embedding #{length(ent_rows)} entities for soft-resolution…")

Enum.chunk_every(ent_rows, 64)
|> Enum.each(fn batch ->
  texts = Enum.map(batch, fn [_id, k] -> k end)

  case Embeddings.embed(texts) do
    {:ok, %{vectors: vs}} when length(vs) == length(batch) ->
      Enum.zip(batch, vs)
      |> Enum.each(fn {[id, _k], v} ->
        Repo.query!("UPDATE node SET vec=$2, embed_model='bge-m3-spike' WHERE id=$1", [id, Pgvector.new(v)])
      end)

    _ ->
      IO.puts("    (embed batch failed)")
  end
end)

# near-duplicate entity pairs (cosine >= sim) that exact-key dedup did NOT merge.
pairs =
  q.(
    "SELECT count(*) FROM (SELECT a.id FROM node a JOIN node b ON a.id<b.id " <>
      "AND a.type<>'article' AND b.type<>'article' AND a.vec IS NOT NULL AND b.vec IS NOT NULL " <>
      "AND (1-(a.vec <=> b.vec))>=$1) s",
    [sim]
  )

[[npairs]] = pairs
IO.puts("    near-duplicate entity pairs (cosine>=#{sim}) the exact key MISSED: #{npairs}")
IO.puts("    → soft-resolution candidates (fragmentation node.vec ANN would catch).")

# ── (C) traversal cost on the denser graph ──────────────────────────────────
hubs = q.("SELECT n.id, count(*) d FROM node n JOIN edge e ON e.src=n.id WHERE n.type<>'article' GROUP BY n.id ORDER BY d DESC LIMIT 5", [])
IO.puts("\n(C) traversal cost on enriched graph (top entity hubs):")

if hubs == [] do
  IO.puts("    (no entity hubs — claim edges sparse)")
else
  Enum.each(hubs, fn [id, deg] ->
    times =
      for d <- [1, 2, 3, 4] do
        t0 = System.monotonic_time(:microsecond)
        _ = Traverse.traverse(id, d, scopes: ["group", "public"])
        Float.round((System.monotonic_time(:microsecond) - t0) / 1000, 2)
      end

    IO.puts("    hub #{id} (outdeg #{deg}): depth1-4 = #{Enum.join(times, " / ")} ms")
  end)
end
