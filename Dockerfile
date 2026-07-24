FROM python:3.11-slim@sha256:e031123e3d85762b141ad1cbc56452ba69c6e722ebf2f042cc0dc86c47c0d8b3

ARG DITTO_VERSION=0.0.0
ARG DITTO_REVISION=local
ARG VALIDATOR_COMPATIBILITY_EPOCH=2
ARG VALIDATOR_HEARTBEAT_PROTOCOL=14

LABEL org.opencontainers.image.source="https://github.com/ditto-assistant/ditto-subnet" \
      org.opencontainers.image.version="$DITTO_VERSION" \
      org.opencontainers.image.revision="$DITTO_REVISION" \
      io.heyditto.validator-service="true" \
      io.heyditto.validator.compatibility-epoch="$VALIDATOR_COMPATIBILITY_EPOCH" \
      io.heyditto.validator.heartbeat-protocol="$VALIDATOR_HEARTBEAT_PROTOCOL" \
      io.heyditto.validator.update-protocol="1" \
      io.heyditto.validator.compose-schema="1"

ENV VALIDATOR_EXPECTED_COMPATIBILITY_EPOCH="$VALIDATOR_COMPATIBILITY_EPOCH"

RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY ditto ./ditto
RUN uv sync --frozen --no-dev --extra telemetry

CMD ["uv", "run", "--no-sync", "python", "-m", "ditto.validator"]
