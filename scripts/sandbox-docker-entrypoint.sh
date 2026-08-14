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
# The DOCKER-USER policy permits only the ticket-bound inference broker and
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
  iptables -A DITTO-SANDBOX-EGRESS -m addrtype --dst-type LOCAL -p tcp --dport 11436 -j ACCEPT
  # Hardcoded https://openrouter.ai traffic resolves to this host gateway. NAT
  # has already translated its destination port from 443 to the scorer's local
  # TLS shim, which still sees the original sandbox source IP and therefore
  # enters the same source-bound ticket session as the documented broker URL.
  iptables -A DITTO-SANDBOX-EGRESS -m addrtype --dst-type LOCAL -p tcp --dport "$openrouter_shim_port" -j ACCEPT
  # Some otherwise-capable nested-Docker kernels omit the optional LOG target
  # or rate-limit match. Keep denial logging best-effort so their absence can
  # never abort setup before the mandatory REJECT rules and INPUT/FORWARD hooks.
  if ! iptables -A DITTO-SANDBOX-EGRESS \
    -m limit --limit 12/min --limit-burst 20 \
    -j LOG --log-prefix 'ditto-sandbox-deny ' --log-level warning; then
    printf 'warning: sandbox denial logging unavailable; enforcement remains active\n' >&2
  fi
  # Deny loudly, not silently. A silent DROP costs a denied TCP connect a full
  # SYN timeout (~26.5 s per resolved address), so a harness still pointed at a
  # non-allowlisted model host spends the whole ticket blackholed instead of
  # failing: measured at 53 s of dead time per benchmark check, which is ~4 h of
  # wall clock on a full run and starves every other agent waiting on the slot.
  # An explicit reset/unreachable ends the connect immediately, so the run
  # completes in minutes and the miner sees a legible "connection refused"
  # instead of a timeout. This changes only how fast a denied packet fails —
  # the allowlist above is unchanged, so nothing new becomes reachable.
  iptables -A DITTO-SANDBOX-EGRESS -p tcp -j REJECT --reject-with tcp-reset
  iptables -A DITTO-SANDBOX-EGRESS -j REJECT --reject-with icmp-port-unreachable
  while iptables -D DOCKER-USER -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I DOCKER-USER 1 -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS
  # Traffic to this DinD host itself traverses INPUT, not DOCKER-USER. Apply
  # the same allowlist there so a harness cannot reach dockerd :2375, the
  # scorer control API :8000, metadata, or sibling host services.
  while iptables -D INPUT -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I INPUT 1 -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS

  # Concurrent scorer revisions create one ICC-disabled bridge per run with a
  # random dtj* interface name. The only permitted host destinations remain the
  # trusted local broker; sibling bridges, metadata, RFC1918 services,
  # public egress, and direct DNS are denied by the same chain. The wildcard is
  # an iptables interface-prefix match (trailing '+'), not a shell glob.
  while iptables -D DOCKER-USER -i 'dtj+' -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I DOCKER-USER 1 -i 'dtj+' -j DITTO-SANDBOX-EGRESS
  while iptables -D INPUT -i 'dtj+' -j DITTO-SANDBOX-EGRESS 2>/dev/null; do :; done
  iptables -I INPUT 1 -i 'dtj+' -j DITTO-SANDBOX-EGRESS

  # Redirect only LOCAL port 443, not public HTTPS. Inner Docker adds exactly
  # openrouter.ai to /etc/hosts at the host gateway; every other public address
  # remains non-local and falls through to the deny policy above. REDIRECT in
  # PREROUTING preserves the miner container's source IP for broker binding.
  iptables -t nat -N DITTO-OPENROUTER-SHIM 2>/dev/null || true
  iptables -t nat -F DITTO-OPENROUTER-SHIM
  iptables -t nat -A DITTO-OPENROUTER-SHIM \
    -m addrtype --dst-type LOCAL -p tcp --dport 443 \
    -j REDIRECT --to-ports "$openrouter_shim_port"
  while iptables -t nat -D PREROUTING -i ditto-sandbox0 -j DITTO-OPENROUTER-SHIM 2>/dev/null; do :; done
  iptables -t nat -I PREROUTING 1 -i ditto-sandbox0 -j DITTO-OPENROUTER-SHIM
  while iptables -t nat -D PREROUTING -i 'dtj+' -j DITTO-OPENROUTER-SHIM 2>/dev/null; do :; done
  iptables -t nat -I PREROUTING 1 -i 'dtj+' -j DITTO-OPENROUTER-SHIM
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

# Docker 29 defaults new installations to the containerd image store. Keep the
# classic store so `docker image save` emits the portable archive contract that
# older validator daemons load with the exact signed config digest.
exec dockerd-entrypoint.sh \
  --feature containerd-snapshotter=false \
  --label io.heyditto.dittobench.isolated=true \
  "$@"
