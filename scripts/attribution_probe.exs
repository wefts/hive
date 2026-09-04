# Does a fact served from the real graph carry its attribution? (learner-eval, kernel step)
#
# Model-free and read-only. It exercises the REAL reader against the REAL staging graph --
# not a fixture -- because the four unit tests prove the shape and only this proves the
# data. No model is loaded, so it runs with the ML stack down.
#
#   SWARM_ENV=staging eval "$(hive/scripts/kernel-measure-env)" \
#     mise exec -C swarm/kernel -- mix run --no-start hive/scripts/attribution_probe.exs
#
# Prints nothing that identifies a machine: counts, relations and ORIGIN FAMILIES only
# (the part before the first ':'), because this file's output gets quoted into the journal.

# Only the database, deliberately: starting the whole app would bind the gRPC port the
# running kernel already holds, and the reader needs nothing but Repo.
Logger.configure(level: :warning)
{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:pgvector)
{:ok, _} = Swarm.Repo.start_link()

alias Swarm.Graph.Network
alias Swarm.Repo

scopes =
  Ecto.Adapters.SQL.query!(
    Repo,
    """
    SELECT scope FROM node WHERE scope IS NOT NULL AND scope <> ''
    UNION SELECT visibility_scope FROM edge WHERE visibility_scope IS NOT NULL AND visibility_scope <> ''
    """,
    []
  ).rows
  |> List.flatten()

# A sample of real host subjects that actually have a proxmox-attested edge.
%{rows: subjects} =
  Ecto.Adapters.SQL.query!(
    Repo,
    """
    SELECT DISTINCT n.key
      FROM edge_provenance ep
      JOIN edge e ON e.id = ep.edge_id
      JOIN node n ON n.id = e.src
     WHERE ep.origin LIKE 'proxmox:%' AND n.key LIKE 'net:host:%'
     ORDER BY n.key
     LIMIT 40
    """,
    []
  )

facts =
  subjects
  |> List.flatten()
  |> Enum.flat_map(&Network.neighborhood(&1, scopes, min_corroboration: 1))

family = fn source -> source |> to_string() |> String.split(":") |> hd() end

{attributed, unattributed} = Enum.split_with(facts, &(&1.sources != []))
{scoped, unscoped} = Enum.split_with(facts, &(&1.scope != nil))
stamped = Enum.count(facts, &(&1.observed_at != nil))

IO.puts("subjects probed:     #{length(subjects)}")
IO.puts("facts served:        #{length(facts)}")
IO.puts("with sources:        #{length(attributed)}  (without: #{length(unattributed)})")
IO.puts("with a single scope: #{length(scoped)}  (nil, derived-across-scopes: #{length(unscoped)})")
IO.puts("with observed_at:    #{stamped}")

IO.puts("\nsource families on served facts:")

facts
|> Enum.flat_map(& &1.sources)
|> Enum.map(family)
|> Enum.frequencies()
|> Enum.sort_by(&(-elem(&1, 1)))
|> Enum.each(fn {fam, n} -> IO.puts("  #{fam}: #{n}") end)

# THE join question, asked of the serve path rather than of answer text: is any single
# served fact attested by two different source families?
joined =
  Enum.filter(facts, fn fact ->
    fact.sources |> Enum.map(family) |> Enum.uniq() |> length() >= 2
  end)

IO.puts("\nfacts attested by >= 2 source families (the join): #{length(joined)} of #{length(facts)}")

joined
|> Enum.take(10)
|> Enum.each(fn fact ->
  IO.puts("  #{fact.relation} <- #{inspect(fact.sources |> Enum.map(family) |> Enum.uniq())}")
end)

# Sanity: corroboration counts distinct LINEAGE, sources are distinct ORIGIN. Origin is
# the finer axis, so sources can exceed corroboration but a fact with sources and
# corroboration 0 would mean the two disagree about the same evidence.
mismatched = Enum.count(attributed, &(&1.corroboration < 1))
IO.puts("attributed facts with corroboration < 1 (should be 0): #{mismatched}")

# --- the join, defined two ways, because only one of them can ever be non-zero --------
#
# CORROBORATING join: two source families assert the SAME edge. That is what `sources` on
# one fact can show, and what the loop above counted.
#
# COMPLEMENTARY join: one SUBJECT carries facts from two families -- Proxmox says where a
# machine runs, a document says what it is for. That is the join the campaign actually
# asks for ("what is host X for?"), and no single fact can ever exhibit it.
#
# Distinguishing these matters: if the sources are complementary rather than redundant,
# the corroborating count stays 0 no matter how well identity is reconciled, and chasing
# it would be chasing a number that cannot move.

fam_sql = "split_part(ep.origin, ':', 1)"

%{rows: [[px_only, doc_only, both, total]]} =
  Ecto.Adapters.SQL.query!(
    Repo,
    """
    WITH node_fam AS (
      SELECT n.id, #{fam_sql} AS family
        FROM edge_provenance ep
        JOIN edge e ON e.id = ep.edge_id
        JOIN node n ON n.id IN (e.src, e.dst)
       WHERE ep.origin IS NOT NULL AND n.key LIKE 'net:host:%'
       GROUP BY 1, 2
    ), fams AS (
      SELECT id, array_agg(DISTINCT family) AS fs FROM node_fam GROUP BY 1
    ), tagged AS (
      SELECT id,
             'proxmox' = ANY(fs) AS px,
             (fs && ARRAY['confluence','wiki','mediawiki']) AS doc
        FROM fams
    )
    SELECT count(*) FILTER (WHERE px AND NOT doc),
           count(*) FILTER (WHERE doc AND NOT px),
           count(*) FILTER (WHERE px AND doc),
           count(*)
      FROM tagged
    """,
    []
  )

IO.puts("\nCOMPLEMENTARY join, per host node (the metric the campaign actually needs):")
IO.puts("  hypervisor-attested only: #{px_only}")
IO.puts("  document-attested only:   #{doc_only}")
IO.puts("  BOTH (the join):          #{both}  of #{total} attributed host nodes")

# Why no node can be in both: are the two sides even talking about the same relations?
%{rows: vocab} =
  Ecto.Adapters.SQL.query!(
    Repo,
    """
    WITH node_fam AS (
      SELECT n.id, #{fam_sql} AS family
        FROM edge_provenance ep
        JOIN edge e ON e.id = ep.edge_id
        JOIN node n ON n.id IN (e.src, e.dst)
       WHERE ep.origin IS NOT NULL AND n.key LIKE 'net:host:%'
       GROUP BY 1, 2
    ), fams AS (
      SELECT id, array_agg(DISTINCT family) AS fs FROM node_fam GROUP BY 1
    ), sides AS (
      SELECT id, CASE WHEN 'proxmox' = ANY(fs) THEN 'hypervisor' ELSE 'documents' END AS side
        FROM fams
    )
    SELECT s.side, e.type, count(DISTINCT s.id) AS hosts
      FROM sides s JOIN edge e ON e.src = s.id
     WHERE e.reward >= 0 AND e.type <> 'is_a'
     GROUP BY 1, 2 ORDER BY 1, 3 DESC
    """,
    []
  )

IO.puts("\nrelation vocabulary by side (hosts asserting it) -- the reason for the number above:")
Enum.each(vocab, fn [side, type, hosts] -> IO.puts("  #{side}: #{type} (#{hosts})") end)
