FROM python:3.12-slim@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates docker-cli git openssl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /bin/

WORKDIR /app/workers/screener
COPY workers/screener/pyproject.toml workers/screener/uv.lock workers/screener/README.md workers/screener/LICENSE ./
COPY packages/ditto-screening-protocol /app/packages/ditto-screening-protocol
COPY workers/screener/ditto_screener ./ditto_screener
COPY workers/screener/tools/l2_analyzer.py ./tools/l2_analyzer.py
RUN uv sync --frozen --no-dev

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["/app/workers/screener/.venv/bin/python", "-m", "ditto_screener.health"]

CMD ["/app/workers/screener/.venv/bin/python", "-m", "ditto_screener"]
