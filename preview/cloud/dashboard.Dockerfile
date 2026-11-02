FROM node:22.23.4-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS build
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
