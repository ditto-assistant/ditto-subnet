# OpenClaw is pinned by both the exact npm version and package-lock integrity.
# Upstream v2026.7.33 resolves to commit b60a4e9fa97cddf1869a1866d879b3051783cf12.
FROM node:24.22.1-bookworm-slim@sha256:55fb5f5a527ea2c5c4a95f963e48ef90aa38c7b346054fdc8ee9957a2b55d205

WORKDIR /opt/adapter
# Install from manifests first (cacheable). Keep npm lifecycle hooks disabled
# (--ignore-scripts); copy and run the scoped nested-floor repair afterward so
# OpenClaw's published shrinkwrap cannot leave vulnerable copies under
# node_modules/openclaw/node_modules (Omar #3455) without busting the ci layer
# when only scripts/ change (Ditto Medium).
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts && npm cache clean --force
COPY scripts ./scripts
RUN node ./scripts/force-patched-transitives.mjs

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
