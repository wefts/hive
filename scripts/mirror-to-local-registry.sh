#!/usr/bin/env bash
# Mirror every runtime image the stack needs into a LOCAL registry on :5000,
# so the stack can boot fully offline (see docker-compose.offline.yml).
# Run once while online. The registry's data volume (swarm_registry_data) is the
# portable artifact — carry it (or the host) to an air-gapped machine.
#
# Models are NOT mirrored here: they live on disk and are bind-mounted RO by the
# ollama service (238 GB under /usr/share/ollama/.ollama/models).
set -euo pipefail

REGISTRY="${REGISTRY:-localhost:5000}"

# Runtime images. App images are built locally; base images come from the Smile
# public mirror (matches what compose pulls). Mirrored under flat names that
# docker-compose.offline.yml references (localhost:5000/<basename>).
IMAGES=(
  "swarm-kernel:0.1.0"
  "swarm-ml:0.1.0"
  "${HUB_REGISTRY:-dockerhub.smile.fr}/ollama/ollama:0.30.6"
  "${HUB_REGISTRY:-dockerhub.smile.fr}/pgvector/pgvector:pg16"
)

# Start the registry if not running.
if ! curl -sf "http://${REGISTRY}/v2/" >/dev/null 2>&1; then
  docker run -d --name local-registry --restart unless-stopped \
    -p 5000:5000 -v swarm_registry_data:/var/lib/registry registry:2 >/dev/null
  for _ in $(seq 1 15); do curl -sf "http://${REGISTRY}/v2/" >/dev/null 2>&1 && break; sleep 1; done
fi

for src in "${IMAGES[@]}"; do
  # Ensure the source is present locally as a tag (a digest-pinned compose pull
  # may not leave the :tag). Locally-built app images are already present.
  docker image inspect "$src" >/dev/null 2>&1 || docker pull "$src"
  dst="${REGISTRY}/$(basename "$src")"
  docker tag "$src" "$dst"
  docker push "$dst"
  echo "mirrored: $dst"
done

# --- Build bases (for offline BUILD) --------------------------------------
# Copied with `imagetools create` so the multi-arch INDEX digest is preserved
# (pull+tag+push would drop the index and break the @sha256 pins). Mirrored
# PATH-PRESERVING (strip only the registry host), so an offline build with
# HUB_REGISTRY/DHI_REGISTRY/UV_REGISTRY=localhost:5000 resolves them by digest.
HUB="${HUB_REGISTRY:-dockerhub.smile.fr}"
DHI="${DHI_REGISTRY:-dhi.smile.fr}"
BASES=(
  "${HUB}/hexpm/elixir:1.19.5-erlang-28.5-alpine-3.23.4"
  "${DHI}/alpine-base:3.23"
  "${DHI}/python:3.13-debian13-dev"
  "${DHI}/python:3.13-debian13"
  "ghcr.io/astral-sh/uv:0.11.23"
)
for src in "${BASES[@]}"; do
  dst="${REGISTRY}/${src#*/}"          # strip the registry host, keep the path
  docker buildx imagetools create --tag "$dst" "$src"
  echo "mirrored base: $dst"
done

echo "catalog:"; curl -s "http://${REGISTRY}/v2/_catalog"
