FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3 AS download

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# The formerly published container tags are unavailable. Fetch the same
# official releases, and verify their published hashes before executing them.
# Hashes come from the matching .sha256sum assets on each GitHub release.
ARG TARGETARCH
RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) server_sha=eef6581f6509f43ece007a6f2eb4c5e3ce41498c8956e919a7ac7b4b170fa431; client_sha=ea4a453be116071ab1ccbd24eb8755bf0579649f41a7b94ab9e68571bb9f4a1e ;; \
      arm64) server_sha=ec9f48fee97dee7e6ce8fe9f576471bcd52380c035091d8b075b64d606124c7e; client_sha=62a7d4cc0ea37e7a5b564e0991057d4fec2a563566511fabe4ee5c1568f5e00b ;; \
      *) echo "Unsupported preview architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 --speed-limit 1024 --speed-time 30 --retry 3 --retry-connrefused --silent --show-error --output /usr/local/bin/minio "https://github.com/minio/minio/releases/download/RELEASE.2025-07-23T15-54-02Z/minio.linux-${arch}.RELEASE.2025-07-23T15-54-02Z" \
    && echo "$server_sha  /usr/local/bin/minio" | sha256sum --check \
    && curl --fail --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 300 --speed-limit 1024 --speed-time 30 --retry 3 --retry-connrefused --silent --show-error --output /usr/local/bin/mc "https://github.com/minio/mc/releases/download/RELEASE.2025-07-21T05-28-08Z/mc.linux-${arch}.RELEASE.2025-07-21T05-28-08Z" \
    && echo "$client_sha  /usr/local/bin/mc" | sha256sum --check \
    && chmod 0755 /usr/local/bin/minio /usr/local/bin/mc

FROM debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3
COPY --from=download /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=download /usr/local/bin/minio /usr/local/bin/mc /usr/local/bin/
EXPOSE 9000
ENTRYPOINT ["/usr/local/bin/minio"]
CMD ["server", "/data"]
