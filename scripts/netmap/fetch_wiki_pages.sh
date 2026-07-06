#!/usr/bin/env bash
# Fetch server-rendered HTML for dynamic wiki pages (whose tables plain_text/1 strips at ingest),
# via the MediaWiki API using the existing BotPassword creds. Writes each page's API JSON (with
# parse.text = rendered HTML) to tmp/wiki_html/<slug>.json for the host-side HTML parser.
#
# LEAK POSTURE: creds sourced from secrets.env (NEVER printed); page list + fetched HTML are
# intranet-private → gitignored tmp/ ONLY. No intranet page names in this committed file (the page
# list is gitignored, like netmap.repos). Read-only fetch (login is required by the wiki's SSO).
#
# Config (gitignored): tmp/wiki_pages.list — one page title per line (# = comment).
set -euo pipefail
umask 077

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"   # hive/
TMP="${NETMAP_TMP:-$here/tmp}"
PAGES="${WIKI_PAGES:-$TMP/wiki_pages.list}"
OUT="$TMP/wiki_html"
mkdir -p "$OUT"

[ -f "$PAGES" ] || { echo "fetch_wiki_pages: no page list at $PAGES (gitignored; one title/line) — nothing to do"; exit 0; }
[ -f "$here/secrets.env" ] || { echo "fetch_wiki_pages: no secrets.env — cannot authenticate"; exit 1; }

set -a; . "$here/secrets.env" 2>/dev/null; set +a
API="${WIKI_URL%/}/api.php"
JAR="$(mktemp)"; trap 'rm -f "$JAR"' EXIT

# BotPassword 2-step login (creds never echoed)
LT=$(curl -s -m 20 -c "$JAR" "${API}?action=query&meta=tokens&type=login&format=json&formatversion=2" \
     | python3 -c "import sys,json;print(json.load(sys.stdin)['query']['tokens']['logintoken'])")
RES=$(curl -s -m 20 -b "$JAR" -c "$JAR" -d "action=login" \
      --data-urlencode "lgname=${WIKI_USER:-${WIKI_ALT_USERNAME:-}}" \
      --data-urlencode "lgpassword=${WIKI_USER_TOKEN:-${WIKI_ALT_TOKEN:-}}" \
      --data-urlencode "lgtoken=${LT}" -d "format=json&formatversion=2" "${API}" \
      | python3 -c "import sys,json;print(json.load(sys.stdin).get('login',{}).get('result'))")
[ "$RES" = "Success" ] || { echo "fetch_wiki_pages: login result=$RES (aborting)"; exit 1; }

n=0
while IFS= read -r page; do
  [ -z "$page" ] && continue
  case "$page" in \#*) continue ;; esac
  slug=$(printf '%s' "$page" | tr '/ ' '__')
  curl -s -m 30 -b "$JAR" \
    "${API}?$(python3 -c "import urllib.parse,sys;print(urllib.parse.urlencode({'action':'parse','page':sys.argv[1],'prop':'text','format':'json','formatversion':'2'}))" "$page")" \
    > "$OUT/$slug.json"
  n=$((n+1))
done < "$PAGES"
echo "fetch_wiki_pages: fetched $n page(s) → $OUT"
