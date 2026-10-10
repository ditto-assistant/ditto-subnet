# Bases are digest-pinned so a release cannot silently rebuild this frozen
# compatibility image on different bytes. golang:1.23-alpine already resolved
# to the final 1.23.12 patch; the pin records that exact index rather than
# moving the toolchain (see README.md: do not update this tree in place).
FROM golang:1.23.12-alpine@sha256:383395b794dffa5b53012a212365d40c8e37109a626ca30d6151c8348d380b5f AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/model-relay ./cmd/model-relay
COPY internal/llm ./internal/llm
RUN CGO_ENABLED=0 GOOS=linux go test ./cmd/model-relay ./internal/llm && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" \
      -o /out/model-relay ./cmd/model-relay

FROM gcr.io/distroless/static-debian12:nonroot@sha256:afa5c872c891853ca7fcf1f12c3edb23f7eeef36189728842dd51042ff57f7ab AS relay
COPY --from=build /out/model-relay /model-relay
EXPOSE 11435
ENTRYPOINT ["/model-relay"]
