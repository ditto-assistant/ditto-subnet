FROM python:3.11-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY ditto ./ditto
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-sync", "python", "-m", "ditto.validator"]
