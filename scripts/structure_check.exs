# Card 6 spot-check (privacy-safe): did structure survive ingest→segment→chunk on the
# group (intranet) slice? Counts only — NO titles/prose/cell values printed.
#   SWARM_DB_NAME=swarm_slice MIX_ENV=dev mise exec -- mix run --no-start \
#     ../../hive/scripts/structure_check.exs

alias Swarm.Repo
{:ok, _} = Application.ensure_all_started(:ecto_sql)
{:ok, _} = Application.ensure_all_started(:postgrex)
{:ok, _} = Repo.start_link()

c = fn sql -> %{rows: [[n]]} = Repo.query!(sql); n end

group_chunks = c.("SELECT count(*) FROM chunk k JOIN node n ON n.id=k.node_id WHERE n.scope='group'")
# a chunk that preserved a pipe-table row (two+ pipes on a line)
table_chunks = c.("SELECT count(*) FROM chunk k JOIN node n ON n.id=k.node_id WHERE n.scope='group' AND k.text ~ '\\|[^\\n]*\\|'")
# a chunk that preserved a fenced code block
code_chunks = c.("SELECT count(*) FROM chunk k JOIN node n ON n.id=k.node_id WHERE n.scope='group' AND k.text LIKE '%```%'")
# a chunk that carries an ATX heading (section structure)
heading_chunks = c.("SELECT count(*) FROM chunk k JOIN node n ON n.id=k.node_id WHERE n.scope='group' AND k.text ~ '(^|\\n)#+ '")
seg = c.("SELECT count(DISTINCT segmenter) FROM content c JOIN node n ON n.id=c.node_id WHERE n.scope='group' AND c.segmenter='structured-v1'")
# group nodes whose body contains a table, and how many of those produced a table-bearing chunk
nodes_with_table = c.("SELECT count(*) FROM content c JOIN node n ON n.id=c.node_id WHERE n.scope='group' AND c.body ~ '\\|[^\\n]*\\|'")

IO.puts("== Card 6 structure spot-check (group/intranet slice, privacy-safe) ==")
IO.puts("group chunks total:            #{group_chunks}")
IO.puts("  with a pipe-table row:       #{table_chunks}")
IO.puts("  with a fenced code block:    #{code_chunks}")
IO.puts("  with an ATX heading:         #{heading_chunks}")
IO.puts("group nodes whose BODY has a table: #{nodes_with_table}")
IO.puts("segmenter on group content is structured-v1?: #{seg > 0}")
IO.puts(if table_chunks > 0 or code_chunks > 0,
  do: "\nRESULT: structure SURVIVED into chunks (tables/code not flattened).",
  else: "\nRESULT: no structured chunks — structure lost or slice has none.")
