FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates docker-cli git openssl ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 screener \
    && useradd --uid 65532 --gid screener --create-home \
        --home-dir /home/screener --shell /usr/sbin/nologin screener

COPY --from=ghcr.io/astral-sh/uv@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /bin/

WORKDIR /app/workers/screener
COPY workers/screener/pyproject.toml workers/screener/uv.lock workers/screener/README.md workers/screener/LICENSE ./
COPY packages/ditto-screening-protocol /app/packages/ditto-screening-protocol
COPY workers/screener/ditto_screener ./ditto_screener
COPY workers/screener/tools/l2_analyzer.py ./tools/l2_analyzer.py
RUN uv sync --frozen --no-dev

ENV HOME=/home/screener \
    PYTHONDONTWRITEBYTECODE=1
USER 65532:65532

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["/app/workers/screener/.venv/bin/python", "-m", "ditto_screener.health"]

CMD ["/app/workers/screener/.venv/bin/python", "-m", "ditto_screener"]
