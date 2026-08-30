# Build context: the `miners/` directory in a source checkout, or the root of
# the archive produced by `dittobench-unified-miner submit`. The unified crate
# uses the two sibling reference kits as explicit path dependencies so one
# submitted source archive builds one image without copying their agent logic.
FROM rust:1-trixie@sha256:1bcff4befb740599103a2c7cb51058e14479b2e35e3a34a3f0dc4ede09927488 AS builder
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /app

COPY dittobench-starter-kit ./dittobench-starter-kit
COPY dittobench-coding-starter-kit ./dittobench-coding-starter-kit
COPY dittobench-unified-starter-kit ./dittobench-unified-starter-kit

WORKDIR /app/dittobench-unified-starter-kit
RUN cargo build --locked --release --bin dittobench-unified-miner

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
