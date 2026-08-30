FROM rust:1-trixie@sha256:1bcff4befb740599103a2c7cb51058e14479b2e35e3a34a3f0dc4ede09927488 AS builder
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /app

# Compile third-party dependencies against stub sources first, so this layer
# is cached on Cargo.{toml,lock} alone and edits to src/ only rebuild the
# crate itself instead of the whole dependency tree.
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src/bin \
    && touch src/lib.rs \
    && printf 'fn main() {}\n' > src/bin/dittobench-coding-miner.rs \
    && cargo build --locked --release --bin dittobench-coding-miner \
    && rm -rf src

COPY src ./src
# touch: guarantee the real sources are newer than the stub fingerprints, so
# cargo cannot treat the stub-built targets as fresh. find keeps this working
# in forks that drop optional targets such as src/lib.rs.
RUN find src -type f -name '*.rs' -exec touch {} + \
    && cargo build --locked --release --bin dittobench-coding-miner

FROM debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'dittobench:x:65532:65532:DittoBench runtime:/app:/usr/sbin/nologin\n' >> /etc/passwd \
    && printf 'dittobench:x:65532:\n' >> /etc/group
WORKDIR /app
COPY --from=builder /app/target/release/dittobench-coding-miner /usr/local/bin/dittobench-coding-miner
USER dittobench:dittobench
EXPOSE 8080
ENTRYPOINT ["dittobench-coding-miner"]
CMD ["--port", "8080"]
