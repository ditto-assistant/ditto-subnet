FROM python:3.12-slim@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b

ARG DITTO_VERSION=0.0.0
ARG DITTO_REVISION=local
ARG VALIDATOR_COMPATIBILITY_EPOCH=2
ARG VALIDATOR_HEARTBEAT_PROTOCOL=19

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
COPY packages/ditto-screening-protocol ./packages/ditto-screening-protocol
RUN uv sync --frozen --no-dev --extra telemetry

CMD ["uv", "run", "--no-sync", "python", "-m", "ditto.validator"]
