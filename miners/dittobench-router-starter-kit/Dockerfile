FROM rust:1-trixie@sha256:931c2c0531a8b143e044f0f3914dfe802f15bb3d1955f0b657e9359327ac2ffc AS builder
ENV CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /app

# Compile third-party dependencies against stub sources first, so this layer
# is cached on Cargo.{toml,lock} alone and edits to src/ only rebuild the
# crate itself instead of the whole dependency tree.
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src/bin \
    && touch src/lib.rs \
    && printf 'fn main() {}\n' > src/bin/dittobench-router-miner.rs \
    && cargo build --locked --release --bin dittobench-router-miner \
    && rm -rf src

COPY src ./src
# touch: guarantee the real sources are newer than the stub fingerprints, so
# cargo cannot treat the stub-built targets as fresh. find keeps this working
# in forks that drop optional targets such as src/lib.rs.
RUN find src -type f -name '*.rs' -exec touch {} + \
    && cargo build --locked --release --bin dittobench-router-miner

FROM debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && printf 'dittobench:x:65532:65532:DittoBench runtime:/app:/usr/sbin/nologin\n' >> /etc/passwd \
    && printf 'dittobench:x:65532:\n' >> /etc/group
WORKDIR /app
COPY --from=builder /app/target/release/dittobench-router-miner /usr/local/bin/dittobench-router-miner
USER dittobench:dittobench
EXPOSE 8080
# The scored router image runs with a read-only rootfs and no writable mounts
# except a tmpfs; the binary keeps all state in memory and needs no scratch
# directory. The relay base URL and single-use ticket arrive as environment
# variables at run time; no provider credential is ever baked into the image.
ENTRYPOINT ["dittobench-router-miner"]
CMD ["--port", "8080"]
