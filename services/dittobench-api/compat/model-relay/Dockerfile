FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/model-relay ./cmd/model-relay
COPY internal/llm ./internal/llm
RUN CGO_ENABLED=0 GOOS=linux go test ./cmd/model-relay ./internal/llm && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" \
      -o /out/model-relay ./cmd/model-relay

FROM gcr.io/distroless/static-debian12:nonroot AS relay
COPY --from=build /out/model-relay /model-relay
EXPOSE 11435
ENTRYPOINT ["/model-relay"]
