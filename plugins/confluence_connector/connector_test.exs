# Hermetic tests for the Confluence connector (no network, no DB). Injected `:http`
# fixture funs exercise the full fetch→event path + the pure parsers. Run from
# swarm/kernel so the kernel app (Swarm.Ports.Connector behaviour) is compiled:
#
#   mise exec -- mix run --no-start \
#     -r ../../hive/plugins/confluence_connector/confluence_connector.ex \
#     ../../hive/plugins/confluence_connector/connector_test.exs
#
# (paths relative to swarm/kernel). Exits non-zero on any failure — real signal.

ExUnit.start(autorun: false)

defmodule Hive.Confluence.ConnectorTest do
  use ExUnit.Case, async: true

  alias Hive.Confluence.Connector

  # --- fixtures -------------------------------------------------------------

  @page_101 %{
    "id" => "101",
    "type" => "page",
    "status" => "current",
    "title" => "Runbook: Deploy",
    "body" => %{
      "storage" => %{
        "representation" => "storage",
        "value" =>
          "<p>Deploy via <code>task ship</code>.</p>" <>
            "<ac:structured-macro ac:name=\"info\"><ac:rich-text-body><p>note</p></ac:rich-text-body></ac:structured-macro>" <>
            "<p>See <ac:link><ri:page ri:content-title=\"Rollback\" /></ac:link> &amp; logs.</p>" <>
            "<table><tbody><tr><td>step</td></tr></tbody></table>"
      }
    },
    "ancestors" => [%{"id" => "1", "title" => "Space Home"}, %{"id" => "50", "title" => "Operations"}],
    "metadata" => %{"labels" => %{"results" => [%{"name" => "ops"}]}},
    "version" => %{"when" => "2024-03-02T10:15:30.000Z"},
    "_links" => %{"webui" => "/spaces/OPS/pages/101"}
  }

  # archived → must be skipped (glpi lesson: label-filter deprecated/archive)
  @page_102 %{
    "id" => "102",
    "type" => "page",
    "status" => "current",
    "title" => "Old decommissioned service",
    "body" => %{"storage" => %{"value" => "<p>obsolete content here, plenty long</p>"}},
    "ancestors" => [],
    "metadata" => %{"labels" => %{"results" => [%{"name" => "archive"}]}},
    "version" => %{"when" => "2020-01-01T00:00:00.000Z"},
    "_links" => %{"webui" => "/x"}
  }

  # too short after strip → must be skipped (empty/stub page)
  @page_103 %{
    "id" => "103",
    "type" => "page",
    "status" => "current",
    "title" => "Stub",
    "body" => %{"storage" => %{"value" => "<p>hi</p>"}},
    "ancestors" => [],
    "metadata" => %{"labels" => %{"results" => []}},
    "version" => %{"when" => "2024-01-01T00:00:00.000Z"},
    "_links" => %{"webui" => "/y"}
  }

  @page_201 %{
    "id" => "201",
    "type" => "page",
    "status" => "current",
    "title" => "Second page with enough body text to survive the length filter",
    "body" => %{"storage" => %{"value" => "<p>Plenty of readable prose on the second page.</p>"}},
    "ancestors" => [%{"id" => "1", "title" => "Space Home"}],
    "metadata" => %{"labels" => %{"results" => []}},
    "version" => %{"when" => "2024-04-04T00:00:00.000Z"},
    "_links" => %{"webui" => "/z"}
  }

  defp page1_body(opts \\ []) do
    next = Keyword.get(opts, :next, true)
    links = if next, do: %{"next" => "/rest/api/content?start=50"}, else: %{}

    %{"results" => [@page_101, @page_102, @page_103], "start" => 0, "limit" => 50, "size" => 3, "_links" => links}
    |> JSON.encode!()
  end

  defp page2_body do
    %{"results" => [@page_201], "start" => 50, "limit" => 50, "size" => 1, "_links" => %{}}
    |> JSON.encode!()
  end

  # http stub: route by whether the URL carries the start=50 cursor.
  defp http_two_pages do
    fn url ->
      if String.contains?(url, "start=50"), do: {:ok, page2_body()}, else: {:ok, page1_body()}
    end
  end

  defp opts(extra \\ []) do
    Keyword.merge([http: http_two_pages(), scope: "group", space: "OPS", base_url: "https://example.test"], extra)
  end

  # --- describe -------------------------------------------------------------

  test "describe/0 names a confluence connector" do
    d = Connector.describe()
    assert d.name == "confluence"
    assert d.kind == :connector
  end

  # --- pure parsers ---------------------------------------------------------

  test "strip_storage/1 unescapes entities, drops tags/macros/tables, keeps prose" do
    prose = Connector.strip_storage(@page_101["body"]["storage"]["value"])
    assert prose =~ "Deploy via task ship"
    assert prose =~ "See"
    assert prose =~ "& logs"
    refute prose =~ "<"
    refute prose =~ "step"
    refute prose =~ "ac:"
  end

  test "extract_links/1 pulls ri:page content-title targets" do
    assert Connector.extract_links(@page_101["body"]["storage"]["value"]) == ["Rollback"]
  end

  test "auth_header/2 builds HTTP Basic" do
    assert Connector.auth_header("u@x", "tok") ==
             {~c"authorization", ~c"Basic " ++ String.to_charlist(Base.encode64("u@x:tok"))}
  end

  # --- fetch → events -------------------------------------------------------

  test "fetch/2 emits one event per surviving page with provenance, body, edges" do
    {:ok, page} = Connector.fetch(:start, opts())

    # 102 (archive) and 103 (too short) are filtered → only 101 survives page 1
    assert [event] = page.events
    assert event.provenance == "confluence:101"
    assert %DateTime{} = event.occurred_at

    page_entity = Enum.find(event.entities, &(&1.key == "Runbook: Deploy"))
    assert page_entity.type == "article"
    assert page_entity.scope == "group"
    assert page_entity.content =~ "Deploy via task ship"

    assert Enum.any?(event.entities, &(&1.key == "Rollback" and &1.content == ""))
    assert %{from: "Runbook: Deploy", to: "Rollback", type: "links_to"} in event.relations
    # immediate parent = last ancestor
    assert %{from: "Runbook: Deploy", to: "Operations", type: "child_of"} in event.relations
  end

  test "fetch/2 follows _links.next then stops at :done" do
    {:ok, p1} = Connector.fetch(:start, opts())
    assert p1.cursor != :done
    assert p1.cursor["start"] == 50
    refute p1.truncated?

    {:ok, p2} = Connector.fetch(p1.cursor, opts())
    assert p2.cursor == :done
    assert [%{provenance: "confluence:201"}] = p2.events
  end

  test "fetch/2 surfaces a max_pages ceiling as truncated?, never a silent cap" do
    {:ok, p1} = Connector.fetch(:start, opts(max_pages: 1))
    assert p1.cursor == :done
    assert p1.truncated?
  end
end

case ExUnit.run() do
  %{failures: 0} -> :ok
  _ -> System.halt(1)
end
