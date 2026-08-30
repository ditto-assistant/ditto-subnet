# Build context: the `miners/` directory in a source checkout, or the root of
# the archive produced by `dittobench-unified-miner submit`. The unified crate
# uses the two sibling reference kits as explicit path dependencies so one
# submitted source archive builds one image without copying their agent logic.
FROM rust:1-trixie@sha256:1bcff4befb740599103a2c7cb51058e14479b2e35e3a34a3f0dc4ede09927488 AS builder
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /app

# Compile third-party dependencies against stub crates first, so this layer is
# cached on the three Cargo.{toml,lock} pairs alone and source edits only
# rebuild the first-party crates instead of the whole dependency tree.
COPY dittobench-starter-kit/Cargo.toml dittobench-starter-kit/Cargo.lock ./dittobench-starter-kit/
COPY dittobench-coding-starter-kit/Cargo.toml dittobench-coding-starter-kit/Cargo.lock ./dittobench-coding-starter-kit/
COPY dittobench-unified-starter-kit/Cargo.toml dittobench-unified-starter-kit/Cargo.lock ./dittobench-unified-starter-kit/
RUN set -eux; \
    mkdir -p dittobench-starter-kit/src/bin \
        dittobench-coding-starter-kit/src/bin \
        dittobench-unified-starter-kit/src; \
    touch dittobench-starter-kit/src/lib.rs \
        dittobench-coding-starter-kit/src/lib.rs; \
    printf 'fn main() {}\n' > dittobench-starter-kit/src/bin/dittobench-miner.rs; \
    printf 'fn main() {}\n' > dittobench-coding-starter-kit/src/bin/dittobench-coding-miner.rs; \
    printf 'fn main() {}\n' > dittobench-unified-starter-kit/src/main.rs; \
    cargo build --locked --release --bin dittobench-unified-miner \
        --manifest-path dittobench-unified-starter-kit/Cargo.toml; \
    rm -rf dittobench-starter-kit/src \
        dittobench-coding-starter-kit/src \
        dittobench-unified-starter-kit/src

COPY dittobench-starter-kit ./dittobench-starter-kit
COPY dittobench-coding-starter-kit ./dittobench-coding-starter-kit
COPY dittobench-unified-starter-kit ./dittobench-unified-starter-kit

WORKDIR /app/dittobench-unified-starter-kit
# touch: guarantee the real sources are newer than the stub fingerprints, so
# cargo cannot treat the stub-built targets as fresh. find keeps this working
# in forks that reshape the optional targets.
RUN find ../dittobench-starter-kit/src ../dittobench-coding-starter-kit/src src \
        -type f -name '*.rs' -exec touch {} + \
    && cargo build --locked --release --bin dittobench-unified-miner

FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'dittobench:x:65532:65532:DittoBench runtime:/app:/usr/sbin/nologin\n' >> /etc/passwd \
    && printf 'dittobench:x:65532:\n' >> /etc/group
WORKDIR /app
COPY --from=builder /app/dittobench-unified-starter-kit/target/release/dittobench-unified-miner /usr/local/bin/dittobench-unified-miner
RUN chown 65532:65532 /app

ENV DITTOBENCH_DB=/app/dittobench.db
EXPOSE 8080
USER dittobench:dittobench

ENTRYPOINT ["dittobench-unified-miner"]
CMD ["serve", "--port", "8080"]
