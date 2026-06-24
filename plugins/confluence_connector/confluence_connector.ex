defmodule Hive.Confluence.Connector do
  @moduledoc """
  A **private** Confluence connector (swarm ADR-5), a `hive/plugins` adapter that
  the kernel auto-loads via `Swarm.Plugins` (in-process dev-adapter mode, ports.md;
  ADR-11). It is self-contained: source-specific parsing (Confluence storage-XHTML)
  stays OUT of the public kernel — only the typed `Swarm.Ports.Connector` contract
  crosses the boundary, never this source's knowledge.

  Ported in *spirit* from the glpi-agent `kb/confluence.py` prototype (auth flow,
  pagination, field paths, the label/empty-page filters), reimplemented as clean
  idiomatic Elixir against the `fetch/2` contract — not a transliteration.

  ## Shape (the contrast vs the prose Wikipedia slice)

  Confluence pages are **HTML storage-format** with tables, code, and `<ac:…>`
  macros, served from the CQL search endpoint behind **HTTP Basic** auth and
  paginated by an **opaque cursor** in `_links.next` (a manual `start` offset is
  silently ignored — it re-returns page 1, verified live). The kernel-driven cursor
  is therefore the next URL the API hands back. `totalSize`, when present, lets the
  `Sync` loop reconcile coverage (ADR-5 §3).

  ## What it emits

  One ingest event per surviving page:

      %{
        provenance: "confluence:<id>",       # evidential origin = stable page id
        occurred_at: <version.when, UTC>,
        entities: [
          %{type: "article", key: <title>, scope: <env scope>, content: <prose>},
          %{type: "article", key: <link/parent title>, scope: …, content: ""}, …
        ],
        relations: [
          %{from: <title>, to: <linked title>, type: "links_to"}, …
          %{from: <title>, to: <parent title>, type: "child_of"}   # immediate ancestor
        ]
      }

  Link/parent targets are identity-only **stubs** (same `(type, key)` as their own
  page) so the idempotent upsert folds them into one node when that page lands —
  the connector's entity-resolution surface (titles; the soft-match case is the
  kernel `entity-resolution` seam, ADR-13).

  Archived/deprecated pages (by label) and empty/stub pages (prose `< @min_body`)
  are skipped — the glpi-agent's hard-won filters.

  ## Config (`opts`)

  - `:base_url` — Confluence base (default `CONFLUENCE_URL` env).
  - `:space` — restrict to a space key (CQL `space = …`); omit for the whole site.
  - `:scope` — node/edge scope (default `"group"` — intranet, never `"public"`).
  - `:limit` — page size (default 50).
  - `:max_pages` — stop after N pages; a still-`next` source then flags
    `truncated?: true` (no silent cap). `nil` = exhaust.
  - `:since` — a `DateTime` watermark → delta (CQL `lastmodified >= …`).
  - `:http` — injectable `(url -> {:ok, body} | {:error, term})` for hermetic
    tests; defaults to a real Basic-auth `:httpc` GET reading creds from the env.
  """

  @behaviour Swarm.Ports.Connector

  require Logger

  @search "/wiki/rest/api/content/search"
  @expand "body.storage,ancestors,metadata.labels,version"
  @limit 50
  @min_body 20
  @skip_labels ~w(deprecated archive archived obsolete)
  @epoch ~U[1970-01-01 00:00:00Z]
  @user_agent ~c"swarm-kernel-confluence/0.1"

  @impl true
  def describe,
    do: %{name: "confluence", kind: :connector, source: "confluence", sync_modes: [:full, :delta]}

  @impl true
  def fetch(:start, opts), do: do_fetch(initial_url(opts), 1, opts)

  def fetch(%{"url" => next_url, "__page" => page_num}, opts),
    do: do_fetch(next_url, page_num, opts)

  defp do_fetch(request_url, page_num, opts) do
    http = Keyword.get(opts, :http, &http_get/1)
    scope = Keyword.get(opts, :scope, "group")

    with {:ok, body} <- http.(request_url),
         {:ok, json} <- decode(body) do
      events = json |> Map.get("results", []) |> Enum.flat_map(&page_to_event(&1, scope))
      {:ok, paginate(events, json, page_num, opts)}
    end
  end

  # --- pagination -----------------------------------------------------------

  # The CQL search endpoint paginates by an OPAQUE cursor in `_links.next` (a manual
  # `start` offset is NOT honored — it silently re-returns page 1, verified live).
  # So the kernel-driven cursor IS the next URL Confluence hands us. `:max_pages` is
  # surfaced as `truncated?` (no silent cap); `totalSize` rides as `:total` for the
  # Sync loop's coverage reconciliation (ADR-5 §3).
  defp paginate(events, json, page_num, opts) do
    next = get_in(json, ["_links", "next"])
    max_pages = Keyword.get(opts, :max_pages)

    page =
      case Map.get(json, "totalSize") do
        n when is_integer(n) -> %{events: events, total: n}
        _ -> %{events: events}
      end

    cond do
      is_nil(next) ->
        Map.merge(page, %{cursor: :done, truncated?: false})

      is_integer(max_pages) and page_num >= max_pages ->
        Map.merge(page, %{cursor: :done, truncated?: true})

      true ->
        link_base = get_in(json, ["_links", "base"]) || default_link_base(opts)
        cursor = %{"url" => join_next(link_base, next), "__page" => page_num + 1}
        Map.merge(page, %{cursor: cursor, truncated?: false})
    end
  end

  # `_links.next` is relative to the `/wiki` CONTEXT path, not the host root — so
  # resolve it against `_links.base` (which Confluence returns as `<host>/wiki`),
  # falling back to `base_url + /wiki`. Resolving against the bare host drops `/wiki`
  # and 404s (verified live).
  defp default_link_base(opts), do: String.trim_trailing(base_url(opts), "/") <> "/wiki"

  defp join_next(link_base, next) do
    cond do
      String.starts_with?(next, "http") -> next
      String.starts_with?(next, "/") -> String.trim_trailing(link_base, "/") <> next
      true -> String.trim_trailing(link_base, "/") <> "/" <> next
    end
  end

  # --- page → event ---------------------------------------------------------

  defp page_to_event(page, scope) do
    title = page |> Map.get("title", "") |> String.trim()
    xhtml = get_in(page, ["body", "storage", "value"]) || ""
    prose = strip_storage(xhtml)

    cond do
      archived?(page) -> []
      title == "" or String.length(prose) < @min_body -> []
      true -> [build_event(page, title, xhtml, prose, scope)]
    end
  end

  defp build_event(page, title, xhtml, prose, scope) do
    targets =
      xhtml
      |> extract_links()
      |> Enum.reject(&(&1 == "" or &1 == title))
      |> Enum.uniq()

    parent = parent_title(page)
    stub = &%{type: "article", key: &1, scope: scope, content: ""}

    entities =
      [%{type: "article", key: title, scope: scope, content: prose}] ++
        Enum.map(targets, stub) ++ parent_entities(parent, stub)

    relations =
      Enum.map(targets, &%{from: title, to: &1, type: "links_to"}) ++
        parent_relations(title, parent)

    %{
      provenance: "confluence:#{Map.get(page, "id")}",
      occurred_at: occurred_at(page),
      entities: entities,
      relations: relations
    }
  end

  defp parent_entities(nil, _stub), do: []
  defp parent_entities(parent, stub), do: [stub.(parent)]

  defp parent_relations(_title, nil), do: []
  defp parent_relations(title, parent), do: [%{from: title, to: parent, type: "child_of"}]

  # Immediate parent = the last ancestor (Confluence orders root → … → parent).
  defp parent_title(page) do
    case page |> Map.get("ancestors", []) |> List.last() do
      %{"title" => t} when is_binary(t) and t != "" -> String.trim(t)
      _ -> nil
    end
  end

  defp archived?(page) do
    page
    |> get_in(["metadata", "labels", "results"])
    |> List.wrap()
    |> Enum.any?(fn l -> String.downcase(Map.get(l, "name", "")) in @skip_labels end)
  end

  defp occurred_at(page) do
    with s when is_binary(s) <- get_in(page, ["version", "when"]),
         {:ok, dt, _off} <- DateTime.from_iso8601(s) do
      dt
    else
      _ -> @epoch
    end
  end

  # --- storage-XHTML → prose / links ----------------------------------------

  @doc """
  Strip Confluence storage-XHTML to readable prose: drop `<table>…</table>` blocks
  (their cells aren't prose), strip every remaining tag (including `<ac:…>` macro
  and `<ri:…>` ref markup), then unescape XML entities and collapse whitespace.
  Structure-aware body handling (tables/code as data) is the Phase-2 segmenter's
  job; here we emit clean text and let `extract_links/1` keep the link structure.
  """
  @spec strip_storage(String.t()) :: String.t()
  def strip_storage(xhtml) when is_binary(xhtml) do
    xhtml
    |> String.replace(~r/<table\b.*?<\/table>/su, " ")
    |> String.replace(~r/<[^>]+>/u, " ")
    |> unescape()
    |> String.replace(~r/\s+/u, " ")
    |> String.trim()
  end

  @doc "Extract linked page titles from Confluence `<ri:page ri:content-title=\"…\">` refs."
  @spec extract_links(String.t()) :: [String.t()]
  def extract_links(xhtml) when is_binary(xhtml) do
    ~r/<ri:page\b[^>]*\bri:content-title="([^"]*)"/u
    |> Regex.scan(xhtml, capture: :all_but_first)
    |> Enum.map(fn [t | _] -> t |> unescape() |> String.trim() end)
    |> Enum.reject(&(&1 == ""))
  end

  @entities %{
    "&amp;" => "&",
    "&lt;" => "<",
    "&gt;" => ">",
    "&quot;" => "\"",
    "&#39;" => "'",
    "&apos;" => "'",
    "&nbsp;" => " "
  }

  defp unescape(text) do
    Enum.reduce(@entities, text, fn {from, to}, acc -> String.replace(acc, from, to) end)
  end

  # --- request --------------------------------------------------------------

  @doc "HTTP Basic auth header (httpc charlist form)."
  @spec auth_header(String.t(), String.t()) :: {charlist(), charlist()}
  def auth_header(user, token),
    do: {~c"authorization", ~c"Basic " ++ String.to_charlist(Base.encode64("#{user}:#{token}"))}

  defp base_url(opts), do: Keyword.get(opts, :base_url) || System.get_env("CONFLUENCE_URL") || ""

  # The first request; subsequent pages follow the API's `_links.next` cursor.
  defp initial_url(opts) do
    params = %{
      "cql" => cql(opts),
      "expand" => @expand,
      "start" => "0",
      "limit" => to_string(Keyword.get(opts, :limit, @limit))
    }

    String.trim_trailing(base_url(opts), "/") <> @search <> "?" <> URI.encode_query(params)
  end

  # CQL: current pages, optionally one space, optionally a delta watermark.
  defp cql(opts) do
    ["type = page"]
    |> maybe_space(Keyword.get(opts, :space))
    |> maybe_since(Keyword.get(opts, :since))
    |> Enum.join(" AND ")
  end

  defp maybe_space(clauses, nil), do: clauses
  defp maybe_space(clauses, space), do: clauses ++ [~s(space = "#{space}")]

  defp maybe_since(clauses, %DateTime{} = since),
    do: clauses ++ [~s(lastmodified >= "#{Calendar.strftime(since, "%Y/%m/%d %H:%M")}")]

  defp maybe_since(clauses, _), do: clauses

  defp http_get(url) do
    ensure_started()
    user = System.get_env("CONFLUENCE_USER") || ""
    token = System.get_env("CONFLUENCE_TOKEN") || ""

    headers = [
      {~c"user-agent", @user_agent},
      {~c"accept", ~c"application/json"},
      auth_header(user, token)
    ]

    http_opts = [ssl: ssl_opts(), timeout: 30_000, connect_timeout: 15_000]

    case :httpc.request(:get, {String.to_charlist(url), headers}, http_opts, body_format: :binary) do
      {:ok, {{_v, 200, _r}, _h, body}} -> {:ok, body}
      {:ok, {{_v, status, _r}, _h, _body}} -> {:error, {:http_status, status}}
      {:error, reason} -> {:error, {:http, reason}}
    end
  end

  defp ensure_started do
    {:ok, _} = Application.ensure_all_started(:inets)
    {:ok, _} = Application.ensure_all_started(:ssl)
    :ok
  end

  # Verify by default. A self-hosted intranet may present a private-CA cert; a hive
  # operator can opt out with `CONFLUENCE_TLS_INSECURE=1` (private deployment only —
  # never a kernel default). The choice is logged so it is never silent.
  defp ssl_opts do
    if System.get_env("CONFLUENCE_TLS_INSECURE") == "1" do
      Logger.warning(
        "confluence connector: TLS verification DISABLED (CONFLUENCE_TLS_INSECURE=1)"
      )

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
