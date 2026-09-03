#!/usr/bin/env bash
# Scan committed/tracked public-repo content for obvious private material.
# This is intentionally conservative and text-only: it catches the classes that
# have escaped review before, while documenting known safe literals inline.

set -euo pipefail

cd "$(dirname "$0")/.."

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

tracked="$tmp/tracked.txt"
diff="$tmp/diff.patch"
combined="$tmp/combined.txt"
hits="$tmp/hits.txt"

git grep -nI -E '.' -- ':!CHANGELOG.md' > "$tracked" || true

if git rev-parse --verify origin/main >/dev/null 2>&1; then
  git diff --no-ext-diff --find-renames --find-copies origin/main...HEAD -- . > "$diff" || true
else
  : > "$diff"
fi

cat "$tracked" "$diff" > "$combined"

perl -ne '
  chomp;
  next if /^[+-]{3} /;
  next if /^(?:Author|Commit|AuthorDate|CommitDate):/;
  next if /^\s*Co-Authored-By:/i;
  next if /@(?:example\.(?:test|org|com|net)|local\.dev|localhost|invalid)\b/i;
  next if /\b(?:https?:\/\/)?(?:[^[:space:]]+\.)?example\.test\b/i;

  # Operator 2026-09-03: this network no longer exists; keep as a named
  # allowlist entry so future leak reviews do not re-raise it as live infra.
  next if /\bapt\.nebula\.intranet\b/i;

  # Operator 2026-09-03: this is a public Smile registry hostname, not an
  # intranet endpoint. Keep named because it looks private at a glance.
  next if /\bdockerhub\.smile\.fr\b/i;

  # Documentation/test-only IP fixtures and standards constants.
  next if /\b(?:192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}\b/;
  next if /\b10\.20\.30\.\d{1,3}\b/;
  next if /\b10\.0\.0\.0\/8\b/;
  next if /\b172\.16\.0\.0(?:\/12)?\b/;
  next if /\b192\.168\.0\.0\/16\b/;
  next if m{(?:^|[/:])test} && /\b(?:10|172\.(?:1[6-9]|2[0-9]|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\.\d{1,3}(?:\/\d{1,2})?\b/;
  next if m{scripts/netmap/(?:test_|parse_wiki_html\.py)} && /\b10\.0\.0\.\d{1,3}(?:\/\d{1,2})?\b/;

  if (/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i ||
      /\b[A-Z0-9-]+(?:\.[A-Z0-9-]+)*\.intranet\b/i ||
      /\b10\.(?!20\.30\.)\d{1,3}\.\d{1,3}\.\d{1,3}(?:\/\d{1,2})?\b/ ||
      /\b172\.(?:1[6-9]|2[0-9]|3[01])\.(?!16\.0\.0\b)\d{1,3}\.\d{1,3}(?:\/\d{1,2})?\b/ ||
      /\b192\.168\.(?!0\.0\/16\b)\d{1,3}\.\d{1,3}(?:\/\d{1,2})?\b/) {
    print "$_\n";
  }
' "$combined" > "$hits"

if [ -s "$hits" ]; then
  echo "leak-scan: possible private material found:" >&2
  sed -n '1,120p' "$hits" >&2
  count=$(wc -l < "$hits" | tr -d ' ')
  if [ "$count" -gt 120 ]; then
    echo "leak-scan: truncated output at 120 of $count hits" >&2
  fi
  exit 1
fi

echo "leak-scan: OK"
