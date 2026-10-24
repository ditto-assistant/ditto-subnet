FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

RUN apt-get update \
    && apt-get install --no-install-recommends -y bash coreutils findutils ripgrep sed \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 65532 analyzer \
    && useradd --uid 65532 --gid 65532 --no-create-home --home-dir /nonexistent analyzer

COPY --chown=65532:65532 tools/l2_analyzer.py /opt/l2_analyzer.py
COPY --chown=65532:65532 ditto_screener/data/starter-kit-provenance-*.json /opt/starter-manifests/
COPY --chown=65532:65532 ditto_screener/data/vendored-upstream-allowlist.json /opt/starter-manifests/
# Fleet release staging inherits a private umask. The rootless analyzer runs
# as container root so its bind-mounted source maps to the daemon identity;
# make only these immutable, repository-owned inputs readable by that identity.
RUN chmod 0444 /opt/l2_analyzer.py /opt/starter-manifests/*.json

USER 65532:65532
WORKDIR /scratch
ENTRYPOINT ["python3", "-I", "/opt/l2_analyzer.py"]
