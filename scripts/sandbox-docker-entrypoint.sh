#!/bin/sh
set -eu

# Inner sandboxes map host.docker.internal to this daemon's bridge gateway.
# Keep the model sidecars reachable without exposing their keys or mounting the
# validator host's Docker socket.
socat \
  TCP-LISTEN:11434,fork,reuseaddr TCP:ollama:11434 &
socat \
  TCP-LISTEN:11435,fork,reuseaddr TCP:model-relay:11435 &

exec dockerd-entrypoint.sh "$@"
