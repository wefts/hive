#!/usr/bin/env bash
# Generate Python gRPC stubs for the Core API into src/web_channel/_gen.
# Mirrors swarm/Taskfile.yml `proto:cli`. Run from the plugin root:
#   bash scripts/gen-proto.sh
#
# Step 1 refreshes the VENDORED proto from the canonical kernel source when it is
# present on disk (local dev/CI in the wefts workspace), so the vendored copy
# never drifts. In a standalone Docker build context the canonical source is
# absent and we generate from the committed vendored copy.
set -euo pipefail
cd "$(dirname "$0")/.."

CANONICAL="../../../swarm/proto/core.proto"
VENDORED="proto/core.proto"

if [ -f "$CANONICAL" ]; then
  # Preserve our vendored-copy header, append the canonical body (from `syntax`).
  header="$(sed -n '1,/^syntax /p' "$VENDORED" | sed '$d')"
  { printf '%s\n' "$header"; cat "$CANONICAL"; } > "$VENDORED.tmp"
  mv "$VENDORED.tmp" "$VENDORED"
  echo "refreshed $VENDORED from $CANONICAL"
else
  echo "canonical proto not found ($CANONICAL) — generating from vendored $VENDORED"
fi

mkdir -p src/web_channel/_gen
uv run python -m grpc_tools.protoc -I proto \
  --python_out=src/web_channel/_gen \
  --grpc_python_out=src/web_channel/_gen \
  --pyi_out=src/web_channel/_gen \
  proto/core.proto
touch src/web_channel/_gen/__init__.py
# Generated grpc stub uses an absolute import; make it package-relative.
sed -i -E 's/^import core_pb2 as/from . import core_pb2 as/' src/web_channel/_gen/core_pb2_grpc.py
echo "stubs written to src/web_channel/_gen"
