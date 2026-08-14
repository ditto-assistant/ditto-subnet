FROM python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4

RUN apt-get update \
    && apt-get install --no-install-recommends -y docker-cli git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /bin/

WORKDIR /app/workers/screener
COPY workers/screener/pyproject.toml workers/screener/uv.lock workers/screener/README.md workers/screener/LICENSE ./
COPY packages/ditto-screening-protocol /app/packages/ditto-screening-protocol
COPY workers/screener/ditto_screener ./ditto_screener
RUN uv sync --frozen --no-dev

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["uv", "run", "--no-sync", "python", "-m", "ditto_screener.health"]

CMD ["uv", "run", "--no-sync", "python", "-m", "ditto_screener"]
