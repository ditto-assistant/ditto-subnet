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

  # Runtime submissions use a dedicated bridge. The DOCKER-USER policy permits
  # only the two local inference forwarders and replies to established flows;
  # metadata, RFC1918 services, public internet, and direct DNS bypasses are
  # denied. Docker's embedded 127.0.0.11 resolver is handled by dockerd before
  # this forwarding hook. Denials are rate-limited into the daemon/journal log
  # for operator audit without allowing log-flood DoS.
  network_id="$(
    docker network ls \
      --filter name='^ditto-sandbox$' \
      --format '{{.ID}}'
  )" || {
    printf 'failed to list Docker networks; refusing unverified sandbox reuse\n' >&2
    exit 1
  }
  if [ -n "$network_id" ]; then
    network_driver="$(docker network inspect --format '{{.Driver}}' ditto-sandbox)" || {
      printf 'failed to inspect existing ditto-sandbox network\n' >&2
      exit 1
    }
    bridge_name="$(
      docker network inspect \
        --format '{{index .Options "com.docker.network.bridge.name"}}' \
        ditto-sandbox
    )" || {
      printf 'failed to inspect existing ditto-sandbox bridge name\n' >&2
      exit 1
    }
    if [ "$network_driver" != 'bridge' ] || [ "$bridge_name" != 'ditto-sandbox0' ]; then
      printf 'unsafe ditto-sandbox network: driver=%s bridge=%s\n' \
        "$network_driver" "$bridge_name" >&2
      exit 1
    fi
  else
    docker network create \
      --driver bridge \
      --opt com.docker.network.bridge.name=ditto-sandbox0 \
      ditto-sandbox >/dev/null
  fi
  gateway="$(docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' ditto-sandbox)"
  case "$gateway" in
    '' | *[!0-9a-fA-F:.]*)
      printf 'invalid ditto-sandbox gateway: %s\n' "$gateway" >&2
      exit 1
      ;;
  esac
  iptables -N DITTO-SANDBOX-EGRESS 2>/dev/null || true
  iptables -F DITTO-SANDBOX-EGRESS
  iptables -A DITTO-SANDBOX-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A DITTO-SANDBOX-EGRESS -d "$gateway" -p tcp --dport 11434 -j ACCEPT
  iptables -A DITTO-SANDBOX-EGRESS -d "$gateway" -p tcp --dport 11435 -j ACCEPT
  iptables -A DITTO-SANDBOX-EGRESS -m limit --limit 12/min --limit-burst 20 \
    -j LOG --log-prefix 'ditto-sandbox-deny ' --log-level warning
  iptables -A DITTO-SANDBOX-EGRESS -j DROP
  while iptables -D DOCKER-USER -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I DOCKER-USER 1 -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS

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
