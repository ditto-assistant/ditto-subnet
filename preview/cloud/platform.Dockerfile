FROM ghcr.io/astral-sh/uv:0.8.22-python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN uv sync --project apps/platform --frozen
WORKDIR /src/apps/platform
CMD ["/bin/sh", "-ec", "uv run alembic upgrade head && exec uv run python -m ditto.api_server"]
