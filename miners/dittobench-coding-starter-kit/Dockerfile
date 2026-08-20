FROM rust:1-trixie@sha256:1bcff4befb740599103a2c7cb51058e14479b2e35e3a34a3f0dc4ede09927488 AS builder
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /app

COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --locked --release --bin dittobench-coding-miner

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
