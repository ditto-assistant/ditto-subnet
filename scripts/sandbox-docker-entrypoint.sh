#!/bin/sh
set -eu

# Inner sandboxes map host.docker.internal to this daemon's bridge gateway.
# During the bounded v6 transition, keep the frozen relay alongside embeddings
# and the source-bound v7 ticket broker. The relay owns the provider secret;
# miner containers receive no credential.
socat \
  TCP-LISTEN:11434,fork,reuseaddr TCP:ollama:11434 &
socat \
  TCP-LISTEN:11435,fork,reuseaddr TCP:model-relay:11435 &

# Submission builds create a steady stream of images and BuildKit cache in the
# nested daemon's named volume. Keep cleanup inside this isolation boundary:
# mounting the host Docker socket would give the stack control over unrelated
# operator containers. The age filter protects active and recently completed
# benchmarks, and volume prune removes only volumes no container references.
# Create the dedicated runtime bridge if missing and (re)assert its egress
# policy. Idempotent, so it doubles as self-healing: if the network ever
# disappears (a prune, an operator action, a daemon restart), the next call
# recreates it and re-derives the firewall against the fresh gateway.
#
# The DOCKER-USER policy permits only local embeddings, the bounded-transition
# v6 relay, and the ticket-bound v7 inference broker.
# replies to established flows; metadata, RFC1918 services, public internet, and
# direct DNS bypasses are denied. Docker's embedded 127.0.0.11 resolver is
# handled by dockerd before this forwarding hook. Denials are rate-limited into
# the daemon/journal log for operator audit without allowing log-flood DoS.
#
# A transient inspection/creation failure returns non-zero so the caller can
# retry; a genuinely UNSAFE existing network (wrong driver/bridge) or an
# underivable gateway is fatal (exit) — better to restart the container than run
# untrusted code against an unverified egress boundary.
ensure_sandbox_network() {
  network_id="$(
    docker network ls \
      --filter name='^ditto-sandbox$' \
      --format '{{.ID}}'
  )" || {
    printf 'failed to list Docker networks; will retry\n' >&2
    return 1
  }
  if [ -n "$network_id" ]; then
    network_driver="$(docker network inspect --format '{{.Driver}}' ditto-sandbox)" || {
      printf 'failed to inspect existing ditto-sandbox network; will retry\n' >&2
      return 1
    }
    bridge_name="$(
      docker network inspect \
        --format '{{index .Options "com.docker.network.bridge.name"}}' \
        ditto-sandbox
    )" || {
      printf 'failed to inspect existing ditto-sandbox bridge name; will retry\n' >&2
      return 1
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
      --opt com.docker.network.bridge.enable_icc=false \
      ditto-sandbox >/dev/null || {
      printf 'failed to create ditto-sandbox network; will retry\n' >&2
      return 1
    }
  fi
  gateway="$(docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' ditto-sandbox)" || {
    printf 'failed to read ditto-sandbox gateway; will retry\n' >&2
    return 1
  }
  case "$gateway" in
    '' | *[!0-9a-fA-F:.]*)
      printf 'invalid ditto-sandbox gateway: %s\n' "$gateway" >&2
      exit 1
      ;;
  esac
  iptables -N DITTO-SANDBOX-EGRESS 2>/dev/null || true
  iptables -F DITTO-SANDBOX-EGRESS
  iptables -A DITTO-SANDBOX-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A DITTO-SANDBOX-EGRESS -m addrtype --dst-type LOCAL -p tcp --dport 11434 -j ACCEPT
  iptables -A DITTO-SANDBOX-EGRESS -m addrtype --dst-type LOCAL -p tcp --dport 11435 -j ACCEPT
  iptables -A DITTO-SANDBOX-EGRESS -m addrtype --dst-type LOCAL -p tcp --dport 11436 -j ACCEPT
  iptables -A DITTO-SANDBOX-EGRESS -m limit --limit 12/min --limit-burst 20 \
    -j LOG --log-prefix 'ditto-sandbox-deny ' --log-level warning
  iptables -A DITTO-SANDBOX-EGRESS -j DROP
  while iptables -D DOCKER-USER -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I DOCKER-USER 1 -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS
  # Traffic to this DinD host itself traverses INPUT, not DOCKER-USER. Apply
  # the same allowlist there so a harness cannot reach dockerd :2375, the
  # scorer control API :8000, metadata, or sibling host services.
  while iptables -D INPUT -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I INPUT 1 -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS

  # Concurrent scorer revisions create one ICC-disabled bridge per run with a
  # random dtj* interface name. The only permitted host destinations remain the
  # two trusted local endpoints; sibling bridges, metadata, RFC1918 services,
  # public egress, and direct DNS are denied by the same chain. The wildcard is
  # an iptables interface-prefix match (trailing '+'), not a shell glob.
  while iptables -D DOCKER-USER -i 'dtj+' -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I DOCKER-USER 1 -i 'dtj+' -j DITTO-SANDBOX-EGRESS
  while iptables -D INPUT -i 'dtj+' -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I INPUT 1 -i 'dtj+' -j DITTO-SANDBOX-EGRESS
}

# Submission builds create a steady stream of images and BuildKit cache in the
# nested daemon's named volume. Keep cleanup inside this isolation boundary:
# mounting the host Docker socket would give the stack control over unrelated
# operator containers. The age filter protects active and recently completed
# benchmarks, and volume prune removes only volumes no container references.
prune_sandbox_docker() {
  until docker info >/dev/null 2>&1; do
    sleep 5
  done

  # Provision once up front; fail fast (via set -e) if the very first attempt
  # cannot establish the egress boundary.
  ensure_sandbox_network

  while :; do
    # Re-assert the sandbox network + firewall before every prune so a missing
    # or drifted bridge self-heals promptly.
    ensure_sandbox_network ||
      printf 'warning: sandbox network re-provision failed; retrying later\n' >&2

    # IMPORTANT: never prune the whole system, which also removes unused
    # networks. Between benchmarks nothing is attached to ditto-sandbox, so a
    # full prune deletes the bridge and every subsequent harness `docker run`
    # fails with "network ditto-sandbox not found". Reclaim containers, images,
    # and build cache explicitly and leave networks alone.
    if ! docker container prune --force; then
      printf 'warning: sandbox Docker container prune failed; retrying later\n' >&2
    fi
    if ! docker image prune --all --force --filter 'until=24h'; then
      printf 'warning: sandbox Docker image prune failed; retrying later\n' >&2
    fi
    if ! docker builder prune --all --force; then
      printf 'warning: sandbox Docker builder prune failed; retrying later\n' >&2
    fi
    if ! docker volume prune --all --force; then
      printf 'warning: sandbox Docker volume prune failed; retrying later\n' >&2
    fi
    sleep 21600
  done
}

prune_sandbox_docker &

exec dockerd-entrypoint.sh "$@"
