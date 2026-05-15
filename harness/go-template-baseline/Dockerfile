# syntax=docker/dockerfile:1.7

# ─── Build stage ─────────────────────────────────────────────────────────────
FROM golang:1.22-alpine AS build

WORKDIR /src

# Build context is the repository root so the in-tree go.mod replace
# directive can pull the bittensor module from a sibling directory.
COPY go /src/go
COPY harness/go-template-baseline /src/harness/go-template-baseline

WORKDIR /src/harness/go-template-baseline

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags="-s -w" -o /out/harness ./cmd/harness

# ─── Runtime stage ───────────────────────────────────────────────────────────
FROM gcr.io/distroless/static:nonroot

COPY --from=build /out/harness /harness

# Validators launch the container with `docker run --rm -i
# --network=<allow-list> --read-only --tmpfs /tmp:rw,size=...
# -e OPENAI_API_KEY=... <image>`. The binary reads ChallengeRequest
# lines on stdin and writes MinerResponse lines on stdout.
ENTRYPOINT ["/harness"]
