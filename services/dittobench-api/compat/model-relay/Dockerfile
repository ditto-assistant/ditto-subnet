FROM golang:1.26.6-alpine@sha256:af8d6740070b8906d12eae1c3e3ea0957fb63f492051ea05e354c38ef9fe88df AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd/model-relay ./cmd/model-relay
COPY internal/llm ./internal/llm
COPY internal/pprofserver ./internal/pprofserver
RUN CGO_ENABLED=0 GOOS=linux go test ./cmd/model-relay ./internal/llm ./internal/pprofserver && \
    CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" \
      -o /out/model-relay ./cmd/model-relay

FROM gcr.io/distroless/static-debian12:nonroot AS relay
COPY --from=build /out/model-relay /model-relay
EXPOSE 11435
ENTRYPOINT ["/model-relay"]
