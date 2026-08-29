# Multi-stage build for the DittoBench miner harness.
#
# ditto-harness is a PUBLIC git dependency, so a plain
#     docker build -t dittobench-miner .
# just works. The optional BuildKit `gh_token` secret is retained only for
# PRIVATE FORKS of the dependency:
#
#     printf '%s' "$(gh auth token)" > /tmp/gh_token
#     DOCKER_BUILDKIT=1 docker build --secret id=gh_token,src=/tmp/gh_token -t dittobench-miner .
#
# CARGO_NET_GIT_FETCH_WITH_CLI=true makes cargo use the system git (which honors
# the credential rewrite below) instead of its built-in fetcher.

# The Rust image is digest-pinned for reproducibility. Its 1.x toolchain must
# remain at Rust >= 1.85 because the harness dependency tree uses edition2024.
# trixie (glibc 2.41), not bookworm (2.36): ort/onnxruntime prebuilt objects
# reference __isoc23_strtol (glibc >= 2.38), which fails to link on bookworm.
FROM rust:1-trixie@sha256:1bcff4befb740599103a2c7cb51058e14479b2e35e3a34a3f0dc4ede09927488 AS builder
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /app

COPY Cargo.toml Cargo.lock ./

# Compile the locked dependency graph before copying application source. This
# layer remains reusable when a miner changes src/ or fixtures/, which avoids
# rebuilding the full Rust dependency tree for every submission revision.
# The placeholder targets force Cargo to compile both package target shapes;
# clean removes only their artifacts while retaining compiled dependencies.
#
# If a gh_token secret is mounted, pass it to Git through process-local config
# for private harness forks. It never enters ENV, a file, or an image layer.
RUN --mount=type=secret,id=gh_token \
    if [ -s /run/secrets/gh_token ]; then \
      token="$(cat /run/secrets/gh_token)"; \
      export GIT_CONFIG_COUNT=1; \
      export GIT_CONFIG_KEY_0="url.https://x-access-token:${token}@github.com/.insteadOf"; \
      export GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi; \
    mkdir -p src/bin; \
    printf 'pub const CACHE_WARMUP: () = ();\n' > src/lib.rs; \
    printf 'fn main() { let _ = dittobench_starter_kit::CACHE_WARMUP; }\n' > src/bin/dittobench-miner.rs; \
    cargo build --locked --release --bin dittobench-miner; \
    cargo clean --locked --release -p dittobench-starter-kit; \
    rm -rf src

COPY src ./src
COPY fixtures ./fixtures

# --locked builds exactly the committed Cargo.lock (reproducible; fails fast on
# a stale lockfile). Reapply process-local auth only when a private git
# dependency needs it; the default public harness needs no credential.
RUN --mount=type=secret,id=gh_token \
    if [ -s /run/secrets/gh_token ]; then \
      token="$(cat /run/secrets/gh_token)"; \
      export GIT_CONFIG_COUNT=1; \
      export GIT_CONFIG_KEY_0="url.https://x-access-token:${token}@github.com/.insteadOf"; \
      export GIT_CONFIG_VALUE_0="https://github.com/"; \
    fi; \
    cargo build --locked --release --bin dittobench-miner

# --- runtime ---------------------------------------------------------------
# trixie (glibc 2.41) to match the builder: ort/onnxruntime's prebuilt objects
# reference __isoc23_strtol (glibc >= 2.38), which bookworm (2.36) lacks, so a
# bookworm builder/runtime fails to link. Keep both stages on the same release.
FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'dittobench:x:65532:65532:DittoBench runtime:/app:/usr/sbin/nologin\n' >> /etc/passwd \
    && printf 'dittobench:x:65532:\n' >> /etc/group
WORKDIR /app
COPY --from=builder /app/target/release/dittobench-miner /usr/local/bin/dittobench-miner
COPY --chown=65532:65532 fixtures ./fixtures
RUN chown 65532:65532 /app

# Default DB lives in the working dir; mount a volume to persist it.
ENV DITTOBENCH_DB=/app/dittobench.db
EXPOSE 8080
USER dittobench:dittobench

ENTRYPOINT ["dittobench-miner"]
CMD ["serve", "--port", "8080"]
