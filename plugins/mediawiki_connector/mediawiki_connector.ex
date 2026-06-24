defmodule Hive.MediaWiki.Connector do
  @moduledoc """
  A **private** intranet MediaWiki connector (swarm ADR-5), a `hive/plugins` adapter
  the kernel auto-loads via `Swarm.Plugins` (in-process dev-adapter mode, ports.md;
  ADR-11). Distinct from the public `Swarm.Test.WikipediaConnector` reference: same
  MediaWiki API, but a **configurable intranet base**, **non-public scope**, and an
  optional **BotPassword session**. Self-contained — the wikitext/link/canonical
  helpers are carried here (not imported from the kernel) so source-specific parsing
  never enters the public kernel; only the typed port contract crosses the boundary.

  Ported in *spirit* from glpi-agent `kb/wiki.py` (the `allpages` generator, the
  `continue` pagination, the wikitext strip, the BotPassword 2-step login), as clean
  idiomatic Elixir against `fetch/2`.

  ## Auth (best-effort)

  The intranet wiki's `api.php` is SSO-exempt and **anonymous read works**, which is
  enough for the slice. When `WIKI_USER`/`WIKI_USER_TOKEN` are present, `login/1`
  performs the BotPassword 2-step (token → login) and threads the session cookie so
  login-restricted pages are visible; **login failure degrades to anonymous read**
  (logged), never crashes — the kernel's fail-soft ethos.

  ## What it emits — one event per page

      %{
        provenance: "mediawiki:<pageid>",     # evidential origin = stable page id
        occurred_at: <revision timestamp, UTC>,
        entities: [%{type: "article", key: <title>, scope: <env scope>, content: <prose>}, <link stubs>…],
        relations: [%{from: <title>, to: <linked title>, type: "links_to"}, …]
      }

  ## Config (`opts`)

  - `:base_url` — full `api.php` URL (default `WIKI_URL` env + `/api.php`).
  - `:scope` — node/edge scope (default `"group"` — intranet, never `"public"`).
  - `:gaplimit` — pages per API call (default 30). `:max_pages` — ceiling →
    `truncated?` (no silent cap). `:resolve_redirects` — fold source redirects
    at ingest (default true; tests set false). `:cookie` — session cookie string.
  - `:http` — injectable `(url -> {:ok, body} | {:error, term})` for hermetic tests.
  """

  @behaviour Swarm.Ports.Connector

  require Logger

  @nonarticle_prefixes ~w(
    file image fichier category template help wikipedia portal
    user talk special media mediawiki module book draft timedtext
    wikt en simple commons
  )
  @epoch ~U[1970-01-01 00:00:00Z]
  @user_agent ~c"swarm-kernel-mediawiki/0.1"

  @impl true
  def describe,
    do: %{name: "mediawiki", kind: :connector, source: "mediawiki", sync_modes: [:full]}

  @impl true
  def fetch(:start, opts), do: fetch(%{"__page" => 1}, opts)

  def fetch(cursor, opts) when is_map(cursor) do
    {page_num, continue} = Map.pop(cursor, "__page", 1)
    scope = Keyword.get(opts, :scope, "group")
    http = Keyword.get(opts, :http, &http_get(&1, opts))

    with {:ok, body} <- http.(url(continue, opts)),
         {:ok, json} <- decode(body) do
      raw = json |> pages() |> Enum.map(&extract_page/1)

      redirects =
        if Keyword.get(opts, :resolve_redirects, true),
          do: resolve_titles(raw |> Enum.flat_map(& &1.targets) |> Enum.uniq(), opts, http),
          else: %{}

      events = Enum.map(raw, &build_event(&1, redirects, scope))
      {:ok, paginate(events, json, page_num, opts)}
    end
  end

  # --- pagination -----------------------------------------------------------

  defp paginate(events, json, page_num, opts) do
    cont = Map.get(json, "continue")
    max_pages = Keyword.get(opts, :max_pages)

    cond do
      is_nil(cont) ->
        %{events: events, cursor: :done, truncated?: false}

      is_integer(max_pages) and page_num >= max_pages ->
        %{events: events, cursor: :done, truncated?: true}

      true ->
        next = cont |> stringify() |> Map.put("__page", page_num + 1)
        %{events: events, cursor: next, truncated?: false}
    end
  end

  # --- page → event ---------------------------------------------------------

  defp extract_page(page) do
    title = canonical_title(Map.get(page, "title", ""))
    raw = wikitext(page)

    targets =
      raw |> link_targets() |> Enum.reject(&(&1 == "" or &1 == title)) |> Enum.uniq()

    %{
      title: title,
      targets: targets,
      text: plain_text(raw),
      provenance: "mediawiki:#{Map.get(page, "pageid", Map.get(page, "title"))}",
      occurred_at: occurred_at(page)
    }
  end

  defp build_event(%{title: title} = p, redirects, scope) do
    targets =
      p.targets
      |> Enum.map(&Map.get(redirects, &1, &1))
      |> Enum.map(&canonical_title/1)
      |> Enum.reject(&(&1 == "" or &1 == title))
      |> Enum.uniq()

    page_entity = %{type: "article", key: title, scope: scope, content: Map.get(p, :text, "")}
    stubs = Enum.map(targets, &%{type: "article", key: &1, scope: scope, content: ""})
    relations = Enum.map(targets, &%{from: title, to: &1, type: "links_to"})

    %{
      provenance: p.provenance,
      occurred_at: p.occurred_at,
      entities: [page_entity | stubs],
      relations: relations
    }
  end

  defp pages(%{"query" => %{"pages" => pages}}) when is_list(pages), do: pages
  defp pages(%{"query" => %{"pages" => pages}}) when is_map(pages), do: Map.values(pages)
  defp pages(_), do: []

  defp wikitext(page) do
    case get_in(page, ["revisions"]) do
      [rev | _] -> get_in(rev, ["slots", "main", "content"]) || Map.get(rev, "content") || ""
      _ -> ""
    end
  end

  defp occurred_at(page) do
    with [%{"timestamp" => ts} | _] <- get_in(page, ["revisions"]),
         true <- is_binary(ts),
         {:ok, dt, _off} <- DateTime.from_iso8601(ts) do
      dt
    else
      _ -> @epoch
    end
  end

  # --- wikitext: links + canonicalisation -----------------------------------

  @wikilink ~r/\[\[([^\]\|]+)(?:\|[^\]]*)?\]\]/u

  @doc "Canonical internal-link targets from wikitext (skips non-article namespaces)."
  @spec link_targets(String.t()) :: [String.t()]
  def link_targets(wikitext) when is_binary(wikitext) do
    @wikilink
    |> Regex.scan(wikitext, capture: :all_but_first)
    |> Enum.map(fn [target | _] -> target end)
    |> Enum.reject(&nonarticle?/1)
    |> Enum.map(&canonical_title/1)
    |> Enum.reject(&(&1 == ""))
  end

  defp nonarticle?(target) do
    case String.split(target, ":", parts: 2) do
      [prefix, _rest] -> String.downcase(String.trim(prefix)) in @nonarticle_prefixes
      _ -> false
    end
  end

  @doc "Strip wikitext to readable prose (templates, tables, refs, links→labels, markup)."
  @spec plain_text(String.t()) :: String.t()
  def plain_text(wikitext) when is_binary(wikitext) do
    wikitext
    |> String.replace(~r/<!--.*?-->/su, "")
    |> String.replace(~r/<ref[^>]*\/>/su, "")
    |> String.replace(~r/<ref[^>]*>.*?<\/ref>/su, "")
    |> strip_tables()
    |> strip_templates()
    |> drop_media_links()
    |> String.replace(@wikilink, fn match ->
      case Regex.run(~r/\[\[([^\]\|]+)(?:\|([^\]]*))?\]\]/u, match, capture: :all_but_first) do
        [_target, label] when label != "" -> label
        [target] -> target
        [target, ""] -> target
        _ -> ""
      end
    end)
    |> String.replace(~r/'''?/u, "")
    |> String.replace(~r/^=+\s*(.*?)\s*=+\s*$/mu, "\\1")
    |> String.replace(~r/<[^>]+>/u, "")
    |> String.replace(~r/[ \t]+/u, " ")
    |> String.replace(~r/\n{3,}/u, "\n\n")
    |> String.trim()
  end

  defp strip_tables(text), do: String.replace(text, ~r/\{\|.*?\|\}/su, "")

  defp strip_templates(text) do
    new = String.replace(text, ~r/\{\{[^{}]*\}\}/su, "")
    if new == text, do: new, else: strip_templates(new)
  end

  defp drop_media_links(text),
    do: String.replace(text, ~r/\[\[(?:File|Image|Category|Fichier):[^\]]*\]\]/iu, "")

  @doc "Canonicalise a MediaWiki title (url-decode, drop anchor, `_`→space, upcase first)."
  @spec canonical_title(String.t()) :: String.t()
  def canonical_title(title) when is_binary(title) do
    title
    |> URI.decode()
    |> String.split("#", parts: 2)
    |> hd()
    |> String.replace("_", " ")
    |> String.replace(~r/\s+/u, " ")
    |> String.trim()
    |> upcase_first()
  end

  defp upcase_first(""), do: ""

  defp upcase_first(s) do
    {first, rest} = String.split_at(s, 1)
    String.upcase(first) <> rest
  end

  # --- redirect resolution (swarm ADR-13 layer 2) ---------------------------

  defp resolve_titles([], _opts, _http), do: %{}

  defp resolve_titles(titles, opts, http) do
    titles
    |> Enum.chunk_every(50)
    |> Enum.reduce(%{}, fn batch, acc -> Map.merge(acc, resolve_batch(batch, opts, http)) end)
  end

  defp resolve_batch(batch, opts, http) do
    with {:ok, body} <- http.(resolve_url(batch, opts)),
         {:ok, json} <- decode(body) do
      redirect_map(json)
    else
      _ -> %{}
    end
  end

  defp redirect_map(json) do
    hops =
      (get_in(json, ["query", "normalized"]) || []) ++
        (get_in(json, ["query", "redirects"]) || [])

    direct =
      Map.new(hops, fn h ->
        {canonical_title(Map.get(h, "from", "")), canonical_title(Map.get(h, "to", ""))}
      end)

    Map.new(direct, fn {from, _to} -> {from, chase(from, direct)} end)
  end

  defp chase(key, map, seen \\ MapSet.new()) do
    case Map.get(map, key) do
      nil ->
        key

      next ->
        if MapSet.member?(seen, next), do: key, else: chase(next, map, MapSet.put(seen, key))
    end
  end

  # --- requests --------------------------------------------------------------

  defp url(continue, opts) do
    base = Keyword.get(opts, :base_url) || mediawiki_api()
    gaplimit = Keyword.get(opts, :gaplimit, 30)

    params =
      %{
        "action" => "query",
        "generator" => "allpages",
        "gapnamespace" => "0",
        "gaplimit" => to_string(gaplimit),
        "gapfilterredir" => "nonredirects",
        "prop" => "revisions|info",
        "rvprop" => "content|timestamp",
        "rvslots" => "main",
        "inprop" => "url",
        "format" => "json",
        "formatversion" => "2"
      }
      |> Map.merge(stringify(continue))

    base <> "?" <> URI.encode_query(params)
  end

  defp resolve_url(titles, opts) do
    base = Keyword.get(opts, :base_url) || mediawiki_api()

    params = %{
      "action" => "query",
      "titles" => Enum.join(titles, "|"),
      "redirects" => "1",
      "format" => "json",
      "formatversion" => "2"
    }

    base <> "?" <> URI.encode_query(params)
  end

  defp mediawiki_api do
    case System.get_env("WIKI_URL") do
      nil -> ""
      base -> String.trim_trailing(base, "/") <> "/api.php"
    end
  end

  defp stringify(map) when is_map(map) do
    map
    |> Map.delete("__page")
    |> Map.new(fn {k, v} -> {to_string(k), to_string(v)} end)
  end

  @doc """
  Best-effort BotPassword login. Returns `{:ok, cookie}` or `{:error, reason}`;
  the caller threads the cookie into `:cookie`. Used only on the live path —
  failure degrades to anonymous read. Reads `WIKI_USER`/`WIKI_USER_TOKEN` (with
  `WIKI_ALT_USERNAME`/`WIKI_ALT_TOKEN` fallback) from the env, never opts.
  """
  @spec login(keyword()) :: {:ok, String.t()} | {:error, term()}
  def login(opts \\ []) do
    base = Keyword.get(opts, :base_url) || mediawiki_api()
    user = System.get_env("WIKI_USER") || System.get_env("WIKI_ALT_USERNAME") || ""
    pass = System.get_env("WIKI_USER_TOKEN") || System.get_env("WIKI_ALT_TOKEN") || ""

    with {:ok, token, cookie} <- login_token(base) do
      do_login(base, user, pass, token, cookie)
    end
  end

  defp login_token(base) do
    case request(
           :get,
           base <> "?action=query&meta=tokens&type=login&format=json&formatversion=2",
           [],
           ""
         ) do
      {:ok, _status, headers, body} ->
        with {:ok, json} <- decode(body) do
          {:ok, get_in(json, ["query", "tokens", "logintoken"]), cookie_of(headers)}
        end

      err ->
        err
    end
  end

  defp do_login(base, user, pass, token, cookie) do
    form =
      URI.encode_query(%{
        "action" => "login",
        "lgname" => user,
        "lgpassword" => pass,
        "lgtoken" => token,
        "format" => "json",
        "formatversion" => "2"
      })

    headers = [
      {~c"content-type", ~c"application/x-www-form-urlencoded"},
      {~c"cookie", String.to_charlist(cookie)}
    ]

    case request(:post, base, headers, form) do
      {:ok, _s, resp_headers, body} ->
        case decode(body) do
          {:ok, %{"login" => %{"result" => "Success"}}} -> {:ok, cookie_of(resp_headers, cookie)}
          {:ok, other} -> {:error, {:login_failed, get_in(other, ["login", "result"])}}
          err -> err
        end

      err ->
        err
    end
  end

  defp cookie_of(headers, fallback \\ "") do
    set =
      headers
      |> Enum.filter(fn {k, _v} -> String.downcase(to_string(k)) == "set-cookie" end)
      |> Enum.map(fn {_k, v} -> v |> to_string() |> String.split(";", parts: 2) |> hd() end)

    case set do
      [] -> fallback
      pairs -> Enum.join(pairs, "; ")
    end
  end

  defp http_get(url, opts) do
    headers =
      [{~c"user-agent", @user_agent}, {~c"accept", ~c"application/json"}] ++ cookie_header(opts)

    case request(:get, url, headers, "") do
      {:ok, 200, _h, body} -> {:ok, body}
      {:ok, status, _h, _b} -> {:error, {:http_status, status}}
      err -> err
    end
  end

  defp cookie_header(opts) do
    case Keyword.get(opts, :cookie) do
      c when is_binary(c) and c != "" -> [{~c"cookie", String.to_charlist(c)}]
      _ -> []
    end
  end

  # Low-level :httpc with method/headers/body, returning {status, headers, body}.
  defp request(method, url, headers, body) do
    ensure_started()

    ua =
      if List.keyfind(headers, ~c"user-agent", 0),
        do: headers,
        else: [{~c"user-agent", @user_agent} | headers]

    http_opts = [ssl: ssl_opts(), timeout: 30_000, connect_timeout: 15_000]

    req =
      case method do
        :get -> {String.to_charlist(url), ua}
        :post -> {String.to_charlist(url), ua, ~c"application/x-www-form-urlencoded", body}
      end

    case :httpc.request(method, req, http_opts, body_format: :binary) do
      {:ok, {{_v, status, _r}, resp_headers, resp_body}} -> {:ok, status, resp_headers, resp_body}
      {:error, reason} -> {:error, {:http, reason}}
    end
  end

  defp ensure_started do
    {:ok, _} = Application.ensure_all_started(:inets)
    {:ok, _} = Application.ensure_all_started(:ssl)
    :ok
  end

  defp ssl_opts do
    if System.get_env("WIKI_TLS_INSECURE") == "1" do
      Logger.warning("mediawiki connector: TLS verification DISABLED (WIKI_TLS_INSECURE=1)")
      [verify: :verify_none]
    else
      [
        verify: :verify_peer,
        cacerts: :public_key.cacerts_get(),
        depth: 3,
        customize_hostname_check: [match_fun: :public_key.pkix_verify_hostname_match_fun(:https)]
      ]
    end
  end

  defp decode(body) do
    case JSON.decode(body) do
      {:ok, json} when is_map(json) -> {:ok, json}
      {:ok, _} -> {:error, :unexpected_json}
      {:error, reason} -> {:error, {:bad_json, reason}}
    end
  end
end
