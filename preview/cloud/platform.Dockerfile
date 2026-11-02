FROM ghcr.io/astral-sh/uv:0.8.22-python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Resolve dependencies from the manifests alone, before any source is copied.
# A single `COPY . .` ahead of `uv sync` makes the most expensive layer in the
# image depend on every file in the repository, so a README typo re-resolves
# the whole bittensor dependency set. packages/ is in this layer because
# apps/platform/pyproject.toml takes ditto-screening-protocol as a path
# dependency and uv needs its source to install it.
COPY packages ./packages
COPY apps/platform/pyproject.toml apps/platform/uv.lock apps/platform/README.md ./apps/platform/
RUN uv sync --project apps/platform --frozen --no-install-project
COPY . .
RUN uv sync --project apps/platform --frozen
WORKDIR /src/apps/platform
CMD ["/bin/sh", "-ec", "uv run alembic upgrade head && exec uv run python -m ditto.api_server"]
