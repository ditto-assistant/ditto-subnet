# Hermes is pinned to an exact reviewed upstream revision. Bump deliberately;
# a floating main branch would make benchmark scores irreproducible.
FROM python:3.13-slim-bookworm AS builder

ARG HERMES_REV=e57918ac800121cf9c2956fe55e27df3ea80b562
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir \
       "hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@${HERMES_REV}"

FROM python:3.13-slim-bookworm AS runtime
COPY --from=builder /opt/venv /opt/venv
WORKDIR /opt/adapter
COPY pyproject.toml ./
COPY hermes_dittobench ./hermes_dittobench
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

ENV PATH=/opt/venv/bin:$PATH \
    HERMES_HOME=/tmp/hermes \
    PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
ENTRYPOINT ["hermes-dittobench"]
