# Build the generate service from this repo's own packages — no external
# module dependencies, so the image is byte-reproducible from a tag.
FROM golang:1.27.3@sha256:f20d83a83e18dbd2a4714b8ffd4b30b287b6243962dd75608e86abcc63ba4efc AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/generate-service ./cmd/generate-service

# Static binary, no shell: the service has no exec/filesystem needs and runs
# as nonroot. Cloud Run injects PORT; main.go reads it (default 8090).
FROM gcr.io/distroless/static:nonroot@sha256:d62067879de890a1def9da0e7766b7d20810a5e5555cef8506b752437d7a3e88
ARG SOURCE_COMMIT
ARG SOURCE_TAG
LABEL org.opencontainers.image.source="https://github.com/ditto-assistant/ditto-subnet/tree/${SOURCE_COMMIT}/research/dittobench-datagen" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      org.opencontainers.image.version="${SOURCE_TAG}"
COPY --from=build /out/generate-service /generate-service
ENTRYPOINT ["/generate-service"]
