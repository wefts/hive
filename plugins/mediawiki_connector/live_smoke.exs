# Live smoke for the intranet MediaWiki connector. Sanitized output ONLY (counts/
# lengths/types) — never private titles/prose/URLs. Anonymous read (api.php is
# SSO-exempt); base from WIKI_URL env at runtime.
#
#   set -a; . hive/secrets.env; set +a
#   cd swarm/kernel && mise exec -- mix run --no-start \
#     -r ../../hive/plugins/mediawiki_connector/mediawiki_connector.ex \
#     ../../hive/plugins/mediawiki_connector/live_smoke.exs

alias Hive.MediaWiki.Connector

# best-effort login (degrades to anon); report only whether it succeeded
cookie =
  case Connector.login([]) do
    {:ok, c} -> IO.puts("login: OK"); c
    {:error, r} -> IO.puts("login: degraded to anon (#{inspect(r)})"); nil
  end

opts = [gaplimit: 5, max_pages: 1, scope: "group", resolve_redirects: false, cookie: cookie]

case Connector.fetch(:start, opts) do
  {:ok, page} ->
    IO.puts("fetch: OK")
    IO.puts("events on page 1: #{length(page.events)}")
    IO.puts("cursor: #{if page.cursor == :done, do: :done, else: :more}  truncated?: #{page.truncated?}")

    case page.events do
      [e | _] ->
        body = e.entities |> Enum.find(%{}, &(Map.get(&1, :content, "") != "")) |> Map.get(:content, "")
        links = Enum.count(e.relations, &(&1.type == "links_to"))
        IO.puts("--- first event (sanitized) ---")
        IO.puts("provenance prefix: #{e.provenance |> String.split(":") |> hd()}:<id>")
        IO.puts("occurred_at is DateTime?: #{match?(%DateTime{}, e.occurred_at)}")
        IO.puts("entities: #{length(e.entities)}  links_to relations: #{links}")
        IO.puts("body prose length (bytes): #{byte_size(body)}")
        IO.puts("body looks markup-free?: #{not String.contains?(body, "[[") and not String.contains?(body, "{{")}")

      [] ->
        IO.puts("(no events on this page)")
    end

  {:error, reason} ->
    IO.puts("fetch: ERROR #{inspect(reason)}")
end
