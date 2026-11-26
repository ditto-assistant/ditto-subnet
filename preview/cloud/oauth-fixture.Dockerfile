FROM node:22.23.4-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d
COPY preview/cloud/oauth-fixture.cjs /app/oauth-fixture.cjs
RUN install -d -o node -g node /run/preview-oauth
USER node
CMD ["node", "/app/oauth-fixture.cjs"]
