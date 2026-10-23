# Build the generate service from this repo's own packages — no external
# module dependencies, so the image is byte-reproducible from a tag.
FROM golang:1.27.3-bookworm@sha256:972b4110ba0f47539a975c31e52c64765e1ff19f2d74469ae4a595c6676e80a5 AS build
WORKDIR /src
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/generate-service ./cmd/generate-service

# Static binary, no shell: the service has no exec/filesystem needs and runs
# as nonroot. Cloud Run injects PORT; main.go reads it (default 8090).
FROM gcr.io/distroless/static-debian12:nonroot@sha256:f5b485ea962d9bd1186b2f6b3a061191539b905b82ec395de78cbfae51f20e35
ARG SOURCE_COMMIT
ARG SOURCE_TAG
LABEL org.opencontainers.image.source="https://github.com/ditto-assistant/ditto-subnet/tree/${SOURCE_COMMIT}/research/dittobench-datagen" \
      org.opencontainers.image.revision="${SOURCE_COMMIT}" \
      org.opencontainers.image.version="${SOURCE_TAG}"
COPY --from=build /out/generate-service /generate-service
ENTRYPOINT ["/generate-service"]
