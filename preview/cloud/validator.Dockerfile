FROM ghcr.io/astral-sh/uv:0.8.22-python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
# Dependency layer first, as in platform.Dockerfile: copying the whole tree
# ahead of `uv sync` means no layer cache can ever survive a source change.
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
RUN uv sync --frozen --no-install-project
COPY . .
RUN uv sync --frozen
CMD ["/bin/sh", "-ec", "uv run python preview/cloud/write-preview-wallet.py && exec uv run python -m ditto.validator"]
