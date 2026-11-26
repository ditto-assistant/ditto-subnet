#!/bin/sh
set -eu

openrouter_shim_port="${DITTOBENCH_OPENROUTER_SHIM_PORT:-11437}"
case "$openrouter_shim_port" in
  '' | *[!0-9]*)
    printf 'invalid OpenRouter shim port: %s\n' "$openrouter_shim_port" >&2
    exit 1
    ;;
esac
if [ "$openrouter_shim_port" -lt 1024 ] || [ "$openrouter_shim_port" -gt 65535 ]; then
  printf 'OpenRouter shim port must be unprivileged: %s\n' "$openrouter_shim_port" >&2
  exit 1
fi

# dittobench-api runs as uid 65532 and writes only the public CA bundle here.
# The same named volume is visible to this nested Docker daemon, which mounts
# that bundle read-only into miner containers. Private CA/TLS keys never touch
# the shared filesystem.
openrouter_shim_ca_path="${DITTOBENCH_OPENROUTER_SHIM_CA_BUNDLE_PATH:-/var/lib/dittobench-openrouter-shim/ca-bundle.pem}"
openrouter_shim_ca_dir="${openrouter_shim_ca_path%/*}"
mkdir -p "$openrouter_shim_ca_dir"
chown 65532:65532 "$openrouter_shim_ca_dir"
chmod 0755 "$openrouter_shim_ca_dir"

# Bench v9's public transcript commits to a private replay manifest containing
# the per-run blinding key and alias map. The scorer runs as uid 65532 with a
# read-only root filesystem, so the trusted DinD side of the stack initializes
# the dedicated named volume before dockerd can become healthy and unblock the
# scorer. Keep this separate from the public CA bundle and never mount it into a
# miner container.
private_artifact_dir="${DITTOBENCH_PRIVATE_ARTIFACT_DIR:-/var/lib/dittobench-private-artifacts}"
case "$private_artifact_dir" in
  /*) ;;
  *)
    printf 'private artifact directory must be absolute: %s\n' "$private_artifact_dir" >&2
    exit 1
    ;;
esac
mkdir -p "$private_artifact_dir"
chown 65532:65532 "$private_artifact_dir"
chmod 0700 "$private_artifact_dir"

# The official DinD entrypoint selects nft or legacy iptables before invoking
# dockerd. Dockerfile.sandbox-docker places our wrapper earlier on PATH, so it
# installs the firewall using that same backend before starting the real daemon.

# Docker 29 defaults new installations to the containerd image store. Keep the
# classic store so `docker image save` emits the portable archive contract that
# older validator daemons load with the exact signed config digest.
#
# Name `dockerd` and its listeners explicitly. When the first argument is an
# option, the upstream dind entrypoint prepends its own defaults, and with TLS
# disabled (DOCKER_TLS_CERTDIR="") that default is --host=tcp://0.0.0.0:2375:
# an unauthenticated, privileged daemon API on every interface of this
# container, reachable from any other container on the compose network. The
# only TCP clients are the scorer, which joins this network namespace, and the
# in-container healthcheck, and both use 127.0.0.1. Passing `dockerd` first
# skips only that host-default block; upstream still removes stale PID files,
# runs docker-init as PID 1 and selects the iptables backend.
exec dockerd-entrypoint.sh dockerd \
  --host=unix:///var/run/docker.sock \
  --host=tcp://127.0.0.1:2375 \
  --feature containerd-snapshotter=false \
  --label io.heyditto.dittobench.isolated=true \
  "$@"
