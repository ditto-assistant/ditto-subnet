# OpenClaw is pinned by both the exact npm version and package-lock integrity.
# Upstream v2026.7.1 resolves to commit 2d2ddc43d0dcf71f31283d780f9fe9ff4cc04fe4.
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205

WORKDIR /opt/adapter
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
    && npm cache clean --force

COPY src ./src
COPY plugin ./plugin

ENV HOME=/tmp/openclaw-home \
    OPENCLAW_STATE_DIR=/tmp/openclaw-state \
    DITTOBENCH_DB=/tmp/dittobench.db \
    NODE_ENV=production \
    PORT=8080

USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["node", "src/server.js"]
