# Public software only; build via the Git-archive wrapper, never a worktree context.
FROM golang:1.26.6-alpine@sha256:af8d6740070b8906d12eae1c3e3ea0957fb63f492051ea05e354c38ef9fe88df AS go-build
WORKDIR /src/services/dittobench-api
COPY services/dittobench-api/ ./
COPY research/dittobench-datagen/ /src/research/dittobench-datagen/
RUN go mod download && go mod verify
RUN CGO_ENABLED=0 GOTOOLCHAIN=local go build -mod=readonly -trimpath -buildvcs=false \
    -o /out/dittobench-coding-hosted-worker ./cmd/dittobench-coding-hosted-worker

FROM ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa AS uv
FROM python:3.13.14-slim@sha256:69e18bd8d831d88e0ef70239dc7771ab7c28bc296ae78ac75cde71e60aa4434f AS base
# Use Debian's interpreter at the same absolute path as the approved host.
# Resolved Debian Python/glibc versions and the interpreter hash are recorded
# in the bundle and must match at installation; the host resolves nothing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.13 python3.13-venv git ca-certificates util-linux
COPY --from=uv --chmod=0555 /uv /usr/local/bin/uv
COPY infra/ansible/roles/coding_hosted_runtime/files/runtime-bundle.py /runtime-bundle.py

FROM base AS assemble
ARG SOURCE_REVISION
RUN /usr/bin/python3.13 -c 'import os,re; assert re.fullmatch("[0-9a-f]{40}", os.environ["SOURCE_REVISION"])'
WORKDIR /opt/ditto-coding-hosted/${SOURCE_REVISION}/apps/platform
COPY apps/platform/ ./
COPY packages/ditto-screening-protocol/ /opt/ditto-coding-hosted/${SOURCE_REVISION}/packages/ditto-screening-protocol/
RUN --mount=type=cache,target=/root/.cache/uv UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --python /usr/bin/python3.13
COPY --from=go-build --chmod=0555 /out/dittobench-coding-hosted-worker /opt/ditto-coding-hosted/${SOURCE_REVISION}/bin/dittobench-coding-hosted-worker
RUN mkdir /out && /usr/bin/python3.13 -I /runtime-bundle.py pack \
    --revision "$SOURCE_REVISION" --archive /out/runtime.tar

FROM base AS smoke
ARG SOURCE_REVISION
RUN useradd --uid 10001 --create-home --home-dir /tmp/native-smoke --shell /usr/sbin/nologin native-smoke
COPY --from=assemble /out/runtime.tar /out/runtime.tar
RUN mkdir -m 0755 -p /opt/ditto-coding-hosted && \
    /usr/bin/python3.13 -I /runtime-bundle.py install --revision "$SOURCE_REVISION" \
    --archive /out/runtime.tar --sha256 "$(sha256sum /out/runtime.tar | cut -d' ' -f1)" \
    --confirm 'INSTALL VERIFIED CODING RUNTIME'
RUN /usr/bin/python3.13 -I /runtime-bundle.py verify --revision "$SOURCE_REVISION" \
    --archive /out/runtime.tar --sha256 "$(sha256sum /out/runtime.tar | cut -d' ' -f1)"
# Import/run only harmless entrypoint help under an unprivileged identity.
# No daemon, database, key service, provider, private config or worker is started.
RUN setpriv --reuid=10001 --regid=10001 --clear-groups env -i PATH=/usr/bin:/bin \
    /opt/ditto-coding-hosted/${SOURCE_REVISION}/apps/platform/.venv/bin/python -I -B \
    -c 'import sys; from pathlib import Path; import ditto.api_server.coding_hosted_runtime; import ditto.api_server.coding_private_v2_unwrap; from ditto.api_server.coding_hosted_runtime_io import protected_helper; protected_helper(Path(sys.argv[1]))' \
    /opt/ditto-coding-hosted/${SOURCE_REVISION}/bin/dittobench-coding-hosted-worker
RUN setpriv --reuid=10001 --regid=10001 --clear-groups env -i PATH=/usr/bin:/bin \
    /opt/ditto-coding-hosted/${SOURCE_REVISION}/apps/platform/.venv/bin/python -I -B \
    -c 'import subprocess,sys; p=subprocess.run([sys.argv[1]],capture_output=True); assert p.returncode == 2 and not p.stdout and p.stderr == b"requires --private-shadow-once --config <protected-file>\n"' \
    /opt/ditto-coding-hosted/${SOURCE_REVISION}/bin/dittobench-coding-hosted-worker
RUN printf X >> /opt/ditto-coding-hosted/${SOURCE_REVISION}/bin/dittobench-coding-hosted-worker && \
    if setpriv --reuid=10001 --regid=10001 --clear-groups env -i PATH=/usr/bin:/bin \
      /opt/ditto-coding-hosted/${SOURCE_REVISION}/apps/platform/.venv/bin/python -I -B \
      -c 'import sys; from pathlib import Path; from ditto.api_server.coding_hosted_runtime_io import protected_helper; protected_helper(Path(sys.argv[1]))' \
      /opt/ditto-coding-hosted/${SOURCE_REVISION}/bin/dittobench-coding-hosted-worker 2>/dev/null; then exit 1; fi
RUN cd /out && sha256sum runtime.tar > runtime.tar.sha256

FROM scratch AS export
COPY --from=smoke /out/ /
