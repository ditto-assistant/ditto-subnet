#!/usr/bin/env bash
set -euo pipefail

# RootlessKit/slirp traffic reaches the host OUTPUT chain under this empty
# daemon identity. The default policy is deny-all. An explicitly reviewed
# scorer runtime may reach exactly one host gateway on its fixed capability
# port; private, loopback, metadata, carrier-grade, public, and peer-container
# destinations remain denied.

executor_user="${CODING_EXECUTOR_USER:-ditto-coding-executor}"
executor_uid="$(id -u "$executor_user")"
capability_gateway="${CODING_EXECUTOR_CAPABILITY_GATEWAY:-}"
capability_port="${CODING_EXECUTOR_CAPABILITY_PORT:-11438}"
# Keep the replacement name safely below the kernel's iptables chain-name
# limit even when a six-digit PID is appended below.
chain="DCE-EXEC-EGRESS"
replacement="${chain}-$$"

if [[ -n "$capability_gateway" ]]; then
  [[ "$capability_port" == 11438 ]] || {
    echo 'coding executor capability port is invalid' >&2
    exit 1
  }
  if ! python3 - "$capability_gateway" <<'PY'
import ipaddress
import sys

try:
    address = ipaddress.ip_address(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if (
    address.version != 4
    or address.is_loopback
    or address.is_unspecified
    or address.is_multicast
    or address.is_link_local
):
    raise SystemExit(1)
PY
  then
    echo 'coding executor capability gateway is invalid' >&2
    exit 1
  fi
fi

iptables -N "$replacement"
iptables -A "$replacement" -p udp -d 169.254.169.254/32 --dport 53 -j ACCEPT
iptables -A "$replacement" -p tcp -d 169.254.169.254/32 --dport 53 -j ACCEPT
if [[ -n "$capability_gateway" ]]; then
  iptables -A "$replacement" -p tcp -d "${capability_gateway}/32" --dport "$capability_port" -j ACCEPT
fi
for cidr in \
  10.0.0.0/8 \
  100.64.0.0/10 \
  127.0.0.0/8 \
  169.254.0.0/16 \
  172.16.0.0/12 \
  192.168.0.0/16; do
  iptables -A "$replacement" -d "$cidr" -j REJECT
done
iptables -A "$replacement" -j REJECT

iptables -I OUTPUT 1 -m owner --uid-owner "$executor_uid" -j "$replacement"
while iptables -D OUTPUT -m owner --uid-owner "$executor_uid" -j "$chain" 2>/dev/null; do :; done
iptables -F "$chain" 2>/dev/null || true
iptables -X "$chain" 2>/dev/null || true
iptables -E "$replacement" "$chain"
