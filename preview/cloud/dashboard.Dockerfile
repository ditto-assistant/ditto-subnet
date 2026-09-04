FROM node:22.22.2-bookworm-slim AS build
WORKDIR /src
COPY apps/platform/dashboard/package*.json ./
RUN npm ci
COPY apps/platform/dashboard/ ./
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.29.1-alpine
COPY --from=build /src/dist /usr/share/nginx/html
COPY preview/cloud/nginx-spa.conf /etc/nginx/conf.d/default.conf
