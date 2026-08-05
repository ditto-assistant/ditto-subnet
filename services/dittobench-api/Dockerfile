# Immutable release identity is public metadata, not a credential. Production
# builds pass both args from the signed full-stack release descriptor. Unknown
# defaults keep ordinary local builds working but make /v1/capabilities fail
# closed until an identity is deliberately supplied.
#
# These args are LINKED INTO the binary (see the build stage). The matching ENV
# lines in the runtime targets are a legacy fallback for images that embedded
# nothing: a container recreated against a cached image picks up a new
# environment while still running old code, so an env var can only ever assert an
# identity, never prove one. `docker run <image> version` reports both and flags
# a disagreement.
ARG DITTOBENCH_SOFTWARE_VERSION=unknown
ARG DITTOBENCH_SOURCE_SHA=unknown

# ---- build stage ----
FROM golang:1.26-alpine AS build
WORKDIR /src/services/dittobench-api

# The AST structural fingerprint (internal/astfp) uses go-tree-sitter, which wraps
# the tree-sitter C parser via cgo — so the build needs a C toolchain. build-base
# pulls in gcc + musl-dev.
RUN apk add --no-cache build-base

# The scorer consumes the in-tree generator module. The build context is the
# monorepo root so a generator change and its scorer consumer are one reviewed
# source unit instead of a cross-repository version-bump PR.
COPY services/dittobench-api/go.mod services/dittobench-api/go.sum* ./
COPY research/dittobench-datagen/go.mod /src/research/dittobench-datagen/go.mod
RUN go mod download

COPY services/dittobench-api/ /src/services/dittobench-api/
COPY research/dittobench-datagen/ /src/research/dittobench-datagen/

# Stamp release identity INTO the binary. Declared here (not before COPY) so the
# dependency layers above stay cacheable across identity changes.
#
# The go toolchain's automatic vcs.revision stamping cannot serve this build: a
# git-URL build context arrives as a plain checkout with no .git, and this stage
# has no git binary, so the toolchain records nothing. -ldflags -X is therefore
# the authoritative mechanism; internal/release still reads build info first-class
# for host builds, where it does work.
ARG DITTOBENCH_SOFTWARE_VERSION
ARG DITTOBENCH_SOURCE_SHA
# cgo is required (tree-sitter); link everything statically against musl so the
# resulting binary carries no dynamic deps and still runs on distroless/static.
RUN CGO_ENABLED=1 GOOS=linux go build \
    -ldflags="-s -w -extldflags '-static' \
      -X 'github.com/ditto-assistant/dittobench-api/internal/release.sourceRevision=${DITTOBENCH_SOURCE_SHA}' \
      -X 'github.com/ditto-assistant/dittobench-api/internal/release.softwareVersion=${DITTOBENCH_SOFTWARE_VERSION}'" \
    -o /out/dittobench-api ./cmd/dittobench-api

# ---- validator sandbox runtime ----
# This opt-in target runs the API beside a host Docker daemon. The binary shells
# out to both git and docker when it materializes and runs miner submissions.
# Keep the ordinary runtime below as the default target for hosted practice.
FROM docker:28.3.3-cli-alpine3.22 AS sandbox
ARG DITTOBENCH_SOFTWARE_VERSION
ARG DITTOBENCH_SOURCE_SHA
RUN apk add --no-cache git
COPY --from=build /out/dittobench-api /dittobench-api
ENV HOME=/tmp
# Legacy fallback only; the binary above already carries these values.
ENV DITTOBENCH_SOFTWARE_VERSION=${DITTOBENCH_SOFTWARE_VERSION}
ENV DITTOBENCH_SOURCE_SHA=${DITTOBENCH_SOURCE_SHA}
USER 65532:65532
EXPOSE 8000
ENTRYPOINT ["/dittobench-api", "-port", "8000"]

# ---- runtime stage ----
FROM gcr.io/distroless/static-debian12:nonroot AS runtime
ARG DITTOBENCH_SOFTWARE_VERSION
ARG DITTOBENCH_SOURCE_SHA
COPY --from=build /out/dittobench-api /dittobench-api
# Legacy fallback only; the binary above already carries these values.
ENV DITTOBENCH_SOFTWARE_VERSION=${DITTOBENCH_SOFTWARE_VERSION}
ENV DITTOBENCH_SOURCE_SHA=${DITTOBENCH_SOURCE_SHA}
EXPOSE 8000
ENTRYPOINT ["/dittobench-api", "-port", "8000"]
