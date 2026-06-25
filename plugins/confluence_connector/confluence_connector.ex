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
    md = to_markdown(xhtml)

    cond do
      archived?(page) -> []
      title == "" or String.length(md) < @min_body -> []
      true -> [build_event(page, title, xhtml, md, scope)]
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

  # --- storage-XHTML → swarm_markdown_v1 / links ----------------------------

  @doc """
  Convert Confluence storage-XHTML to the `swarm_markdown_v1` body profile (ADR-14
  §2; the kernel's structure-aware segmenter consumes it). Markup is stripped as
  noise but **structure is preserved**: `<h1-6>` → ATX headings, `<ac:…code…>` →
  fenced code, `<table>` → pipe tables (a flattened table loses row/cell relations),
  `<li>` → `-` lists, `<code>` → inline backticks. Code bodies are placeholder-
  protected so later tag-stripping/unescaping never corrupts them.
  """
  @spec to_markdown(String.t()) :: String.t()
  def to_markdown(xhtml) when is_binary(xhtml) do
    {protected, code} = protect_code(xhtml)

    protected
    |> convert_tables()
    |> convert_headings()
    |> convert_lists()
    |> convert_inline_code()
    |> convert_paragraphs()
    |> strip_tags()
    |> unescape()
    |> normalize_ws()
    |> restore_code(code)
    |> String.trim()
  end

  # Replace `<ac:structured-macro ac:name="code">…<![CDATA[…]]></…>` with a
  # placeholder; return the placeholder text + the fenced blocks to restore later.
  @code_macro ~r/<ac:structured-macro[^>]*ac:name="code".*?<\/ac:structured-macro>/su
  defp protect_code(xhtml) do
    blocks = Regex.scan(@code_macro, xhtml) |> Enum.map(fn [m] -> fence_of(m) end)

    {text, _} =
      Enum.reduce(blocks, {xhtml, 0}, fn _b, {acc, i} ->
        {String.replace(acc, @code_macro, " CODE#{i} ", global: false), i + 1}
      end)

    {text, blocks}
  end

  defp fence_of(macro) do
    lang =
      case Regex.run(~r/<ac:parameter[^>]*ac:name="language"[^>]*>(.*?)<\/ac:parameter>/su, macro) do
        [_, l] -> String.trim(l)
        _ -> ""
      end

    body =
      case Regex.run(~r/<!\[CDATA\[(.*?)\]\]>/su, macro) do
        [_, b] -> b
        _ -> macro |> String.replace(~r/<[^>]+>/u, "") |> String.trim()
      end

    "```#{lang}\n#{body}\n```"
  end

  defp restore_code(text, blocks) do
    blocks
    |> Enum.with_index()
    |> Enum.reduce(text, fn {fence, i}, acc ->
      String.replace(acc, " CODE#{i} ", "\n\n#{fence}\n\n")
    end)
  end

  defp convert_tables(html) do
    Regex.replace(~r/<table[^>]*>(.*?)<\/table>/su, html, fn _, inner ->
      "\n\n" <> table_md(inner) <> "\n\n"
    end)
  end

  defp table_md(inner) do
    rows =
      Regex.scan(~r/<tr[^>]*>(.*?)<\/tr>/su, inner, capture: :all_but_first)
      |> Enum.map(fn [r] -> row_cells(r) end)
      |> Enum.reject(&(&1 == []))

    case rows do
      [head | _] = all ->
        sep = "| " <> Enum.map_join(head, " | ", fn _ -> "---" end) <> " |"
        [md_row(head), sep | Enum.map(tl(all), &md_row/1)] |> Enum.join("\n")

      [] ->
        ""
    end
  end

  defp row_cells(row) do
    Regex.scan(~r/<t[hd][^>]*>(.*?)<\/t[hd]>/su, row, capture: :all_but_first)
    |> Enum.map(fn [c] ->
      c |> strip_tags() |> unescape() |> String.replace(~r/\s+/u, " ") |> String.trim()
    end)
  end

  defp md_row(cells), do: "| " <> Enum.join(cells, " | ") <> " |"

  defp convert_headings(html) do
    Regex.replace(~r/<h([1-6])[^>]*>(.*?)<\/h\1>/su, html, fn _, n, inner ->
      "\n\n" <>
        String.duplicate("#", String.to_integer(n)) <> " " <> clean_inline(inner) <> "\n\n"
    end)
  end

  defp convert_lists(html) do
    items =
      Regex.replace(~r/<li[^>]*>(.*?)<\/li>/su, html, fn _, inner ->
        "\n- " <> clean_inline(inner)
      end)

    String.replace(items, ~r/<\/?[uo]l[^>]*>/u, "\n")
  end

  defp convert_inline_code(html) do
    Regex.replace(~r/<code[^>]*>(.*?)<\/code>/su, html, fn _, inner ->
      "`" <> clean_inline(inner) <> "`"
    end)
  end

  defp convert_paragraphs(html) do
    html
    |> String.replace(~r/<br[^>]*>/u, "\n")
    |> String.replace(~r/<\/?p[^>]*>/u, "\n\n")
  end

  defp clean_inline(html),
    do: html |> strip_tags() |> unescape() |> String.replace(~r/\s+/u, " ") |> String.trim()

  defp strip_tags(html), do: String.replace(html, ~r/<[^>]+>/u, "")

  defp normalize_ws(text) do
    text
    |> String.replace(~r/[ \t]+/u, " ")
    |> String.replace(~r/ *\n */u, "\n")
    |> String.replace(~r/\n{3,}/u, "\n\n")
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
