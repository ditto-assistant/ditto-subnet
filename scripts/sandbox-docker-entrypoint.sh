#!/bin/sh
set -eu

# Inner sandboxes map host.docker.internal to this daemon's bridge gateway.
# Keep the model sidecars reachable without exposing their keys or mounting the
# validator host's Docker socket.
socat \
  TCP-LISTEN:11434,fork,reuseaddr TCP:ollama:11434 &
socat \
  TCP-LISTEN:11435,fork,reuseaddr TCP:model-relay:11435 &

# Submission builds create a steady stream of images and BuildKit cache in the
# nested daemon's named volume. Keep cleanup inside this isolation boundary:
# mounting the host Docker socket would give the stack control over unrelated
# operator containers. The age filter protects active and recently completed
# benchmarks, and volume prune removes only volumes no container references.
prune_sandbox_docker() {
  until docker info >/dev/null 2>&1; do
    sleep 5
  done

  while :; do
    if ! docker system prune --all --force --filter 'until=24h'; then
      printf 'warning: sandbox Docker system prune failed; retrying later\n' >&2
    fi
    if ! docker volume prune --all --force; then
      printf 'warning: sandbox Docker volume prune failed; retrying later\n' >&2
    fi
    sleep 21600
  done
}

prune_sandbox_docker &

exec dockerd-entrypoint.sh "$@"
