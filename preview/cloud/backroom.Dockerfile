FROM node:22.23.4-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d
RUN corepack enable && corepack prepare pnpm@10.34.5 --activate
WORKDIR /src/apps/backroom
COPY apps/backroom/package.json apps/backroom/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY apps/backroom/ ./
COPY preview/cloud/backroom.wrangler.jsonc ./wrangler.preview.jsonc
COPY preview/cloud/backroom-entrypoint.sh /usr/local/bin/backroom-preview
RUN pnpm build
EXPOSE 3000
CMD ["/bin/sh", "/usr/local/bin/backroom-preview"]
