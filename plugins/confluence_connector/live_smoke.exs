# Live smoke for the Confluence connector. Hits the REAL space and prints ONLY
# sanitized diagnostics (counts/lengths/types/booleans) — never private titles,
# prose, or URLs (hard boundary: private data must not enter logs/agent context).
# Creds/base read from the env at runtime (sourced from hive/secrets.env by the
# caller); they never appear here.
#
#   set -a; . hive/secrets.env; set +a
#   cd swarm/kernel && mise exec -- mix run --no-start \
#     -r ../../hive/plugins/confluence_connector/confluence_connector.ex \
#     ../../hive/plugins/confluence_connector/live_smoke.exs

alias Hive.Confluence.Connector

opts = [limit: 10, max_pages: 1, scope: "group"]

case Connector.fetch(:start, opts) do
  {:ok, page} ->
    IO.puts("fetch: OK")
    IO.puts("events (surviving pages on page 1): #{length(page.events)}")
    IO.puts("cursor: #{inspect(page.cursor != :done && :more || :done)}  truncated?: #{page.truncated?}")
    IO.puts("totalSize present?: #{Map.has_key?(page, :total)}")

    case page.events do
      [e | _] ->
        rel_types = e.relations |> Enum.map(& &1.type) |> Enum.frequencies()
        body = e.entities |> Enum.find(%{}, &(Map.get(&1, :content, "") != "")) |> Map.get(:content, "")

        IO.puts("--- first event (sanitized) ---")
        IO.puts("provenance prefix: #{e.provenance |> String.split(":") |> hd()}:<id>")
        IO.puts("occurred_at is DateTime?: #{match?(%DateTime{}, e.occurred_at)}")
        IO.puts("entities: #{length(e.entities)}  (1 page + stubs)")
        IO.puts("relations by type: #{inspect(rel_types)}")
        IO.puts("body prose length (bytes): #{byte_size(body)}")
        IO.puts("body looks tag-free?: #{not String.contains?(body, "<")}")

      [] ->
        IO.puts("(no events survived the label/length filters on this page)")
    end

  {:error, reason} ->
    IO.puts("fetch: ERROR #{inspect(reason)}")
end
