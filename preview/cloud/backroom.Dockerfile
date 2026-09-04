FROM node:22.22.0-bookworm-slim
RUN corepack enable && corepack prepare pnpm@10.34.5 --activate
WORKDIR /src/apps/backroom
COPY apps/backroom/package.json apps/backroom/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY apps/backroom/ ./
COPY preview/cloud/backroom.wrangler.jsonc ./wrangler.preview.jsonc
RUN pnpm build
EXPOSE 3000
CMD ["pnpm", "exec", "wrangler", "dev", "--config", "wrangler.preview.jsonc", "--ip", "0.0.0.0", "--port", "3000"]
