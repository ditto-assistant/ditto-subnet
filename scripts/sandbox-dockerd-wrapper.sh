#!/bin/sh
set -eu

ready=/run/ditto-sandbox-firewall-ready
backend_file=/run/ditto-sandbox-firewall-backend
filter_snapshot=/run/ditto-sandbox-filter-policy
nat_snapshot=/run/ditto-sandbox-nat-policy
worker_file=/run/ditto-sandbox-firewall-worker

select_saved_backend() {
  case "$(cat "$backend_file")" in
    legacy) PATH="/usr/local/sbin/.iptables-legacy:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" ;;
    nft) PATH="/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" ;;
    *) return 1 ;;
  esac
  export PATH
}

check_hooks() {
  for chain in DOCKER-USER INPUT; do
    for bridge in ditto-sandbox0 'dtj+'; do
      iptables -w 5 -C "$chain" -i "$bridge" -j DITTO-SANDBOX-EGRESS >/dev/null 2>&1 || return 1
    done
    rules="$(iptables -w 5 -S "$chain")" || return 1
    first="$(printf '%s\n' "$rules" | sed -n '2p')"
    second="$(printf '%s\n' "$rules" | sed -n '3p')"
    [ "$first" = "-A $chain -i dtj+ -j DITTO-SANDBOX-EGRESS" ] || return 1
    [ "$second" = "-A $chain -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS" ] || return 1
  done
  for bridge in ditto-sandbox0 'dtj+'; do
    iptables -w 5 -t nat -C PREROUTING -i "$bridge" -j DITTO-OPENROUTER-SHIM >/dev/null 2>&1 || return 1
  done
  nat_hooks_first
}

nat_hooks_first() {
  rules="$(iptables -w 5 -t nat -S PREROUTING)" || return 1
  first="$(printf '%s\n' "$rules" | sed -n '2p')"
  second="$(printf '%s\n' "$rules" | sed -n '3p')"
  [ "$first" = '-A PREROUTING -i dtj+ -j DITTO-OPENROUTER-SHIM' ] || return 1
  [ "$second" = '-A PREROUTING -i ditto-sandbox0 -j DITTO-OPENROUTER-SHIM' ]
}

check_forward_hook() {
  rules="$(iptables -w 5 -S FORWARD)" || return 1
  first="$(printf '%s\n' "$rules" | sed -n '2p')"
  [ "$first" = '-A FORWARD -j DOCKER-USER' ]
}

healthcheck() {
  test -f "$ready" || return 1
  worker="$(cat "$worker_file")" || return 1
  case "$worker" in '' | *[!0-9]*) return 1 ;; esac
  kill -0 "$worker" 2>/dev/null || return 1
  state="$(awk '{print $3}' "/proc/$worker/stat")" || return 1
  [ -n "$state" ] && [ "$state" != Z ] || return 1
  select_saved_backend || return 1
  docker info >/dev/null 2>&1 || return 1
  check_hooks || return 1
  check_forward_hook || return 1
  actual_filter="$(iptables -w 5 -S DITTO-SANDBOX-EGRESS)" || return 1
  expected_filter="$(cat "$filter_snapshot")" || return 1
  [ -n "$actual_filter" ] && [ "$actual_filter" = "$expected_filter" ] || return 1
  actual_nat="$(iptables -w 5 -t nat -S DITTO-OPENROUTER-SHIM)" || return 1
  expected_nat="$(cat "$nat_snapshot")" || return 1
  [ -n "$actual_nat" ] && [ "$actual_nat" = "$expected_nat" ] || return 1
  [ "$(docker network inspect --format '{{.Driver}}' ditto-sandbox)" = bridge ] || return 1
  [ "$(docker network inspect --format '{{index .Options "com.docker.network.bridge.name"}}' ditto-sandbox)" = ditto-sandbox0 ] || return 1
}

if [ "${1-}" = '--ditto-healthcheck' ]; then
  healthcheck
  exit
fi

openrouter_shim_port="${DITTOBENCH_OPENROUTER_SHIM_PORT:-11437}"
case "$openrouter_shim_port" in
  '' | *[!0-9]*) printf 'invalid OpenRouter shim port: %s\n' "$openrouter_shim_port" >&2; exit 1 ;;
esac
if [ "$openrouter_shim_port" -lt 1024 ] || [ "$openrouter_shim_port" -gt 65535 ]; then
  printf 'OpenRouter shim port must be unprivileged: %s\n' "$openrouter_shim_port" >&2
  exit 1
fi

# The upstream DinD entrypoint has already selected its iptables backend before
# invoking this wrapper. Use the same PATH for the worker and record it for the
# separate healthcheck process. A stale readiness marker must never survive a
# daemon restart.
rm -f "$ready" "$worker_file"
case "$(iptables --version)" in
  *legacy*) printf 'legacy\n' > "$backend_file" ;;
  *nf_tables*) printf 'nft\n' > "$backend_file" ;;
  *) printf 'unsupported iptables backend\n' >&2; exit 1 ;;
esac

install_firewall() {
  # Docker preserves an existing DOCKER-USER chain at startup. Create it before
  # the daemon can accept work, then install complete chains in atomic table
  # transactions. Never flush a live chain or remove a live interface jump.
  if ! iptables -w 5 -N DOCKER-USER 2>/dev/null; then
    iptables -w 5 -S DOCKER-USER >/dev/null 2>&1 || return 1
  fi
  iptables-restore --wait 5 --noflush <<EOF || return 1
*filter
:DITTO-SANDBOX-EGRESS - [0:0]
-A DITTO-SANDBOX-EGRESS -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A DITTO-SANDBOX-EGRESS -m addrtype --dst-type LOCAL -p tcp --dport 11436 -j ACCEPT
-A DITTO-SANDBOX-EGRESS -m addrtype --dst-type LOCAL -p tcp --dport $openrouter_shim_port -j ACCEPT
-A DITTO-SANDBOX-EGRESS -m limit --limit 12/min --limit-burst 20 -j LOG --log-prefix "ditto-sandbox-deny " --log-level warning
-A DITTO-SANDBOX-EGRESS -p tcp -j REJECT --reject-with tcp-reset
-A DITTO-SANDBOX-EGRESS -j REJECT --reject-with icmp-port-unreachable
COMMIT
EOF
  iptables-restore --wait 5 --noflush <<EOF || return 1
*nat
:DITTO-OPENROUTER-SHIM - [0:0]
-A DITTO-OPENROUTER-SHIM -m addrtype --dst-type LOCAL -p tcp --dport 443 -j REDIRECT --to-ports $openrouter_shim_port
COMMIT
EOF
  for chain in DOCKER-USER INPUT; do
    for bridge in ditto-sandbox0 'dtj+'; do
      if [ "${1-}" = initial ] || ! iptables -w 5 -C "$chain" -i "$bridge" -j DITTO-SANDBOX-EGRESS >/dev/null 2>&1; then
        position=1
        if [ "${1-}" != initial ] && [ "$bridge" = ditto-sandbox0 ] && \
          iptables -w 5 -C "$chain" -i 'dtj+' -j DITTO-SANDBOX-EGRESS >/dev/null 2>&1; then
          position=2
        fi
        iptables -w 5 -I "$chain" "$position" -i "$bridge" -j DITTO-SANDBOX-EGRESS || return 1
      fi
    done
  done
  if ! nat_hooks_first; then
    # First restore only missing hooks, keeping a surviving first-position
    # dtj+ jump in place. Repeated partial losses must not accumulate copies.
    for bridge in ditto-sandbox0 'dtj+'; do
      if ! iptables -w 5 -t nat -C PREROUTING -i "$bridge" -j DITTO-OPENROUTER-SHIM >/dev/null 2>&1; then
        position=1
        if [ "$bridge" = ditto-sandbox0 ] && \
          iptables -w 5 -t nat -C PREROUTING -i 'dtj+' -j DITTO-OPENROUTER-SHIM >/dev/null 2>&1; then
          position=2
        fi
        iptables -w 5 -t nat -I PREROUTING "$position" -i "$bridge" -j DITTO-OPENROUTER-SHIM || return 1
      fi
    done
  fi
  if ! nat_hooks_first; then
    # Hooks exist but have lost priority. Insert only the misplaced hooks;
    # do not delete any active jumps or duplicate a surviving leading hook.
    rules="$(iptables -w 5 -t nat -S PREROUTING)" || return 1
    first="$(printf '%s\n' "$rules" | sed -n '2p')"
    if [ "$first" != '-A PREROUTING -i dtj+ -j DITTO-OPENROUTER-SHIM' ]; then
      iptables -w 5 -t nat -I PREROUTING 1 -i 'dtj+' -j DITTO-OPENROUTER-SHIM || return 1
    fi
    rules="$(iptables -w 5 -t nat -S PREROUTING)" || return 1
    second="$(printf '%s\n' "$rules" | sed -n '3p')"
    if [ "$second" != '-A PREROUTING -i ditto-sandbox0 -j DITTO-OPENROUTER-SHIM' ]; then
      iptables -w 5 -t nat -I PREROUTING 2 -i ditto-sandbox0 -j DITTO-OPENROUTER-SHIM || return 1
    fi
  fi
  check_hooks || return 1
}

ensure_sandbox_network() {
  network_id="$(docker network ls --filter name='^ditto-sandbox$' --format '{{.ID}}')" || return 1
  if [ -n "$network_id" ]; then
    network_driver="$(docker network inspect --format '{{.Driver}}' ditto-sandbox)" || return 1
    bridge_name="$(docker network inspect --format '{{index .Options "com.docker.network.bridge.name"}}' ditto-sandbox)" || return 1
    if [ "$network_driver" != bridge ] || [ "$bridge_name" != ditto-sandbox0 ]; then
      printf 'unsafe ditto-sandbox network: driver=%s bridge=%s\n' "$network_driver" "$bridge_name" >&2
      return 2
    fi
  else
    docker network create --driver bridge --opt com.docker.network.bridge.name=ditto-sandbox0 \
      --opt com.docker.network.bridge.enable_icc=false ditto-sandbox >/dev/null || return 1
  fi
  gateway="$(docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' ditto-sandbox)" || return 1
  case "$gateway" in '' | *[!0-9a-fA-F:.]*) return 2 ;; esac
}

fail_closed() {
  rm -f "$ready"
  printf 'sandbox firewall initialization or reconciliation failed; stopping daemon\n' >&2
  kill -TERM "$daemon_pid" 2>/dev/null || :
  exit 1
}

maintain_sandbox() {
  until docker info >/dev/null 2>&1; do sleep 1; done
  while :; do
    install_firewall || fail_closed
    attempts=0
    while :; do
      if ensure_sandbox_network; then break; else network_status=$?; fi
      # An unsafe network is fatal immediately. API errors get a short,
      # bounded retry while readiness is withheld.
      rm -f "$ready"
      [ "$network_status" -ne 2 ] || fail_closed
      attempts=$((attempts + 1))
      [ "$attempts" -lt 3 ] || fail_closed
      sleep 2
    done
    check_forward_hook || fail_closed
    iptables -w 5 -S DITTO-SANDBOX-EGRESS > "$filter_snapshot.tmp" || fail_closed
    mv -f "$filter_snapshot.tmp" "$filter_snapshot" || fail_closed
    iptables -w 5 -t nat -S DITTO-OPENROUTER-SHIM > "$nat_snapshot.tmp" || fail_closed
    mv -f "$nat_snapshot.tmp" "$nat_snapshot" || fail_closed
    touch "$ready" || fail_closed

    # Never prune networks; the scorer needs the stable bridge between runs.
    docker container prune --force || printf 'warning: container prune failed\n' >&2
    docker image prune --all --force --filter 'until=24h' || printf 'warning: image prune failed\n' >&2
    docker builder prune --all --force || printf 'warning: builder prune failed\n' >&2
    docker volume prune --all --force || printf 'warning: volume prune failed\n' >&2
    sleep 21600
  done
}

# Install the complete policy before dockerd listens. The background worker
# inherits the exact firewall backend selected by the upstream DinD entrypoint.
install_firewall initial
daemon_pid=$$
maintain_sandbox &
worker_pid=$!
printf '%s\n' "$worker_pid" > "$worker_file"
(
  while kill -0 "$worker_pid" 2>/dev/null; do
    state="$(awk '{print $3}' "/proc/$worker_pid/stat")" || break
    [ -n "$state" ] && [ "$state" != Z ] || break
    sleep 1
  done
  rm -f "$ready"
  printf 'sandbox firewall maintenance worker exited; stopping daemon\n' >&2
  kill -TERM "$daemon_pid" 2>/dev/null || :
) &
exec /usr/local/bin/dockerd "$@"
