#!/bin/sh
set -eu

# The outer container retains only a tiny trusted bootstrap/maintenance path.
# dockerd and every miner container run as the image's unprivileged `rootless`
# identity. The outer privilege required by nested rootless Docker is never
# inherited across the uid drop.
executor_uid="$(id -u rootless)"
runtime_dir="/run/user/$executor_uid"
# Trusted maintenance and health clients use the rootless daemon's local Unix
# socket. The API reaches the same daemon over the private Compose network.
export DOCKER_HOST="unix://$runtime_dir/docker.sock"
host_gateway_ip="$(
  ip -o -4 addr show dev eth0 scope global \
    | awk 'NR == 1 { split($4, address, "/"); print address[1] }'
)"
case "$host_gateway_ip" in
  '')
    printf 'invalid rootless sandbox host gateway\n' >&2
    exit 1
    ;;
esac

# RootlessKit emits inner-container traffic from executor_uid in this outer
# network namespace. Permit only established traffic, daemon-local loopback,
# and the ticket-bound broker on the shared namespace address. The trusted API
# runs as another uid, so its platform/OpenRouter traffic is unaffected.
iptables -N DITTO-ROOTLESS-EGRESS 2>/dev/null || true
iptables -F DITTO-ROOTLESS-EGRESS
iptables -A DITTO-ROOTLESS-EGRESS \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A DITTO-ROOTLESS-EGRESS -d 127.0.0.0/8 -j ACCEPT
iptables -A DITTO-ROOTLESS-EGRESS -d "$host_gateway_ip" \
  -p tcp --dport 11436 -j ACCEPT
iptables -A DITTO-ROOTLESS-EGRESS -m limit --limit 12/min --limit-burst 20 \
  -j LOG --log-prefix 'ditto-rootless-deny ' --log-level warning
iptables -A DITTO-ROOTLESS-EGRESS -p tcp -j REJECT --reject-with tcp-reset
iptables -A DITTO-ROOTLESS-EGRESS -j REJECT --reject-with icmp-port-unreachable
while iptables -D OUTPUT -m owner --uid-owner "$executor_uid" \
  -j DITTO-ROOTLESS-EGRESS 2>/dev/null; do :; done
iptables -I OUTPUT 1 -m owner --uid-owner "$executor_uid" \
  -j DITTO-ROOTLESS-EGRESS

prune_sandbox_docker() {
  until docker info >/dev/null 2>&1; do
    sleep 5
  done
  while :; do
    # Request-scoped networks can be idle between API teardown steps, so broad
    # pruning that also deletes networks can race an active run.
    docker container prune --force || true
    docker image prune --all --force --filter 'until=24h' || true
    docker builder prune --all --force || true
    docker volume prune --all --force || true
    sleep 21600
  done
}

prune_sandbox_docker &
socat TCP-LISTEN:11434,reuseaddr,fork \
  EXEC:/usr/local/bin/sandbox-docker-health-once &

mkdir -p "$runtime_dir"
chown rootless:rootless "$runtime_dir"
chmod 0700 "$runtime_dir"

# Docker 29 defaults fresh daemons to the containerd image store, whose
# externally reported image ID is a manifest digest. Screened artifacts retain
# the portable Docker-save contract used by pre-0.42 validators and sign the
# image-config digest instead. Keep the nested executor on the classic store so
# every supported validator generation observes that same immutable identity.
# This changes only the private named volume's storage backend; rootless uid,
# egress, cgroup, and seccomp/AppArmor isolation remain unchanged.
exec su-exec rootless env \
  HOME=/home/rootless \
  XDG_RUNTIME_DIR="$runtime_dir" \
  DOCKER_HOST="$DOCKER_HOST" \
  dockerd-entrypoint.sh --feature containerd-snapshotter=false "$@"
