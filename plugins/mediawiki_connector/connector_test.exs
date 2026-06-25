# Hermetic tests for the intranet MediaWiki connector (no network, no DB). Injected
# `:http` fixture funs exercise the fetch→event path + pure parsers. Run from
# swarm/kernel:
#
#   mise exec -- mix run --no-start \
#     -r ../../hive/plugins/mediawiki_connector/mediawiki_connector.ex \
#     ../../hive/plugins/mediawiki_connector/connector_test.exs

ExUnit.start(autorun: false)

defmodule Hive.MediaWiki.ConnectorTest do
  use ExUnit.Case, async: true

  alias Hive.MediaWiki.Connector

  defp rev(ts, content),
    do: [%{"timestamp" => ts, "slots" => %{"main" => %{"content" => content}}}]

  defp p11,
    do: %{"pageid" => 11, "title" => "Runbook Deploy", "revisions" => rev("2024-03-02T10:15:30Z", "Deploy steps. See [[Rollback]] and [[Monitoring|metrics]].")}

  defp p12,
    do: %{"pageid" => 12, "title" => "Rollback", "revisions" => rev("2024-02-01T00:00:00Z", "How to roll back. {{infobox|x}} '''bold''' text.")}

  defp p13,
    do: %{"pageid" => 13, "title" => "Monitoring", "revisions" => rev("2024-01-01T00:00:00Z", "Dashboards and alerts live here.")}

  defp page1, do: JSON.encode!(%{"query" => %{"pages" => [p11(), p12()]}, "continue" => %{"gapcontinue" => "Monitoring", "continue" => "gapcontinue||"}})
  defp page2, do: JSON.encode!(%{"query" => %{"pages" => [p13()]}})

  defp http do
    fn url ->
      if String.contains?(url, "gapcontinue"), do: {:ok, page2()}, else: {:ok, page1()}
    end
  end

  defp opts(extra \\ []),
    do: Keyword.merge([http: http(), scope: "group", base_url: "https://wiki.test/api.php", resolve_redirects: false], extra)

  test "describe/0 names a mediawiki connector" do
    d = Connector.describe()
    assert d.name == "mediawiki"
    assert d.kind == :connector
  end

  test "canonical_title/1 url-decodes, trims, upcases first" do
    assert Connector.canonical_title("rollback") == "Rollback"
    assert Connector.canonical_title("%21%21%21 (album)") == "!!! (album)"
    assert Connector.canonical_title("foo_bar#sec") == "Foo bar"
  end

  test "link_targets/1 extracts article links, skips File: namespace, keeps labels' targets" do
    wt = "See [[Rollback]] and [[Monitoring|metrics]] plus [[File:x.png]]."
    assert Connector.link_targets(wt) == ["Rollback", "Monitoring"]
  end

  test "plain_text/1 strips templates and bold markup" do
    out = Connector.plain_text("How to roll back. {{infobox|x}} '''bold''' text.")
    refute out =~ "{{"
    refute out =~ "'''"
    assert out =~ "roll back"
    assert out =~ "bold"
  end

  test "plain_text/1 converts wikitext headings to swarm_markdown_v1 ATX headings" do
    out = Connector.plain_text("== Overview ==\n\nbody\n\n=== Details ===\n\nmore")
    assert out =~ "## Overview"
    assert out =~ "### Details"
    refute out =~ "=="
  end

  test "fetch/2 emits one event per page with provenance, scope, body, links" do
    {:ok, page} = Connector.fetch(:start, opts())
    assert length(page.events) == 2

    e = Enum.find(page.events, &(&1.provenance == "mediawiki:11"))
    assert %DateTime{} = e.occurred_at
    pe = Enum.find(e.entities, &(&1.key == "Runbook Deploy"))
    assert pe.scope == "group"
    assert pe.content =~ "Deploy steps"
    assert Enum.any?(e.entities, &(&1.key == "Rollback"))
    assert %{from: "Runbook Deploy", to: "Rollback", type: "links_to"} in e.relations
    assert %{from: "Runbook Deploy", to: "Monitoring", type: "links_to"} in e.relations
  end

  test "fetch/2 follows continue then stops at :done" do
    {:ok, p1} = Connector.fetch(:start, opts())
    assert p1.cursor != :done
    refute p1.truncated?

    {:ok, p2} = Connector.fetch(p1.cursor, opts())
    assert p2.cursor == :done
    assert [%{provenance: "mediawiki:13"}] = p2.events
  end

  test "fetch/2 surfaces a max_pages ceiling as truncated?" do
    {:ok, p1} = Connector.fetch(:start, opts(max_pages: 1))
    assert p1.cursor == :done
    assert p1.truncated?
  end

  test "scope is configurable and never defaults to public" do
    {:ok, page} = Connector.fetch(:start, opts(scope: "private"))
    assert Enum.all?(hd(page.events).entities, &(&1.scope == "private"))
    refute match?(%{scope: "public"}, hd(hd(page.events).entities))
  end
end

case ExUnit.run() do
  %{failures: 0} -> :ok
  _ -> System.halt(1)
end
