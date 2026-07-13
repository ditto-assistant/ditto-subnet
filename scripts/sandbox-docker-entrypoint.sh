#!/bin/sh
set -eu

# Inner sandboxes resolve host.docker.internal to this daemon namespace. Relay
# those ports onward to the physical validator host's model services.
socat TCP-LISTEN:11434,fork,reuseaddr TCP:host.docker.internal:11434 &
socat TCP-LISTEN:11435,fork,reuseaddr TCP:host.docker.internal:11435 &

exec dockerd-entrypoint.sh "$@"
