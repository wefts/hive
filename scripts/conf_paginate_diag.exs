# Diagnose Confluence page-2 fetch failure WITHOUT leaking the intranet host:
# print only the next-URL's path + query KEYS + the error reason.
alias Hive.Confluence.Connector

{:ok, p1} = Connector.fetch(:start, scope: "group", limit: 25)
IO.puts("page1: events=#{length(p1.events)} cursor=#{if p1.cursor == :done, do: :done, else: :more}")

case p1.cursor do
  %{"url" => url} ->
    u = URI.parse(url)
    keys = (u.query || "") |> URI.decode_query() |> Map.keys() |> Enum.sort()
    IO.puts("next path: #{u.path}")
    IO.puts("next query keys: #{inspect(keys)}")

    case Connector.fetch(p1.cursor, scope: "group", limit: 25) do
      {:ok, p2} -> IO.puts("page2: OK events=#{length(p2.events)} cursor=#{if p2.cursor == :done, do: :done, else: :more}")
      {:error, reason} -> IO.puts("page2: ERROR #{inspect(reason)}")
    end

  other ->
    IO.puts("cursor not a url: #{inspect(other)}")
end
