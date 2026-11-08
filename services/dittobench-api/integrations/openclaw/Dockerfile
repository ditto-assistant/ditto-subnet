# OpenClaw is pinned by both the exact npm version and package-lock integrity.
# Upstream v2026.7.33 resolves to commit b60a4e9fa97cddf1869a1866d879b3051783cf12.
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205

WORKDIR /opt/adapter
# Copy manifests and the shrinkwrap repair before install. Keep npm lifecycle
# hooks disabled (--ignore-scripts); invoke the scoped nested-floor repair
# explicitly so OpenClaw's published shrinkwrap cannot leave vulnerable
# copies under node_modules/openclaw/node_modules (see Omar review on #3455).
COPY package.json package-lock.json ./
COPY scripts ./scripts
RUN npm ci --omit=dev --ignore-scripts \
    && node ./scripts/force-patched-transitives.mjs \
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
