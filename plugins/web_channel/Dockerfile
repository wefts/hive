# syntax=docker/dockerfile:1
#
# PRODUCTION image for web_channel (Python/uv, FastAPI + uvicorn, gRPC Core client).
# Dev runs on the host via uv — this image is the packaged artifact only.
#
# Mirrors swarm/ml/Dockerfile: DHI debian13/glibc base (manylinux wheels for
# grpcio/uvloop install cleanly), distroless + non-root (uid 65532) runtime, no
# shell. Unlike ml, the build context is THIS plugin dir — the Core proto is
# vendored at proto/core.proto, so the context is self-contained:
#   docker build -f Dockerfile -t web-channel:0.1.0 .
# Proto stubs are GENERATED here (gitignored, never copied from host) —
# reproducible by construction. Refresh the vendored proto via scripts/gen-proto.sh.
ARG DHI_REGISTRY=dhi.smile.fr
ARG DHI_PY_DEV=${DHI_REGISTRY}/python:3.13-debian13-dev@sha256:8dae89c2868f96d7de0cc06fd9c6e87bb242197f4ce07d5a5c73e0de5f009eb8
ARG DHI_PY_RUN=${DHI_REGISTRY}/python:3.13-debian13@sha256:6e8db8c9c5e95077a0f387c6196dfd92bdd5872370987b73e3d4d843b04f0628
ARG UV_REGISTRY=ghcr.io
ARG UV_IMAGE=${UV_REGISTRY}/astral-sh/uv:0.11.23@sha256:d0a0a753ab981624b49c97abc98821c1c09f4ca69d1ef5cee69c501be3d88479

# uv binary as a named stage — `COPY --from` can't take a variable directly.
FROM ${UV_IMAGE} AS uvbin

# ---- build stage --------------------------------------------------------
FROM ${DHI_PY_DEV} AS build
USER root
COPY --from=uvbin /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON=/opt/python/bin/python3
WORKDIR /app

# Deps as a cached layer (dev group included: grpcio-tools generates the stubs).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Vendored proto contract + service source.
COPY proto ./proto
COPY src ./src

# Generate gRPC stubs from the vendored proto, install the project, prune dev deps.
RUN mkdir -p src/web_channel/_gen \
 && uv run python -m grpc_tools.protoc -I proto \
      --python_out=src/web_channel/_gen \
      --grpc_python_out=src/web_channel/_gen \
      --pyi_out=src/web_channel/_gen \
      proto/core.proto \
 && touch src/web_channel/_gen/__init__.py \
 && sed -i -E 's/^import core_pb2 as/from . import core_pb2 as/' src/web_channel/_gen/core_pb2_grpc.py \
 && uv sync --frozen --no-dev

# Durable conversation-log dir — created here so the runtime COPY --chown gives the
# volume mountpoint to the non-root uid (distroless runtime has no shell to mkdir).
RUN mkdir -p /app/data

# ---- runtime stage (DHI distroless, non-root, no shell) -----------------
FROM ${DHI_PY_RUN} AS runtime
LABEL org.opencontainers.image.title="web-channel" \
      org.opencontainers.image.description="Swarm web_channel: FastAPI/HTMX operator console over the Core API" \
      org.opencontainers.image.version="0.1.0"
ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    SWARM_CORE_ADDR=kernel:50061 \
    SWARM_VIEWER=operator
WORKDIR /app
COPY --from=build --chown=65532:65532 /app /app

# Explicit non-root (the DHI distroless default is already 65532; be explicit so
# the running process never relies on the base image's default uid).
USER 65532:65532
EXPOSE 8080
# Liveness: TCP-connect to the web bind port (bound = serving). No shell in the
# image, so this is exec-form via the interpreter.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import socket; socket.create_connection(('127.0.0.1', 8080), 3).close()"]
ENTRYPOINT ["/app/.venv/bin/uvicorn", "web_channel.main:app", "--host", "0.0.0.0", "--port", "8080"]
