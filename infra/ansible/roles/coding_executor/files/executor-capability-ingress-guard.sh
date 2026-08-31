#!/usr/bin/env bash
set -euo pipefail

# The scorer capability listener is host-side, so INPUT must fail closed even
# after the daemon has a narrow matching OUTPUT exception. An enabled profile
# admits only one reviewed private rootless candidate CIDR to one gateway/port.

gateway="${CODING_EXECUTOR_CAPABILITY_GATEWAY:-}"
source_cidr="${CODING_EXECUTOR_CAPABILITY_SOURCE_CIDR:-}"
port="${CODING_EXECUTOR_CAPABILITY_PORT:-11438}"
chain="DCE-EXEC-INGRESS"
replacement="${chain}-$$"

if [[ -n "$gateway" || -n "$source_cidr" ]]; then
  [[ -n "$gateway" && -n "$source_cidr" && "$port" == 11438 ]] || {
    echo 'coding executor capability ingress configuration is incomplete' >&2
    exit 1
  }
  if ! python3 - "$gateway" "$source_cidr" <<'PY'
import ipaddress
import sys

try:
    gateway = ipaddress.ip_address(sys.argv[1])
    source = ipaddress.ip_network(sys.argv[2], strict=True)
except ValueError:
    raise SystemExit(1)
private_ranges = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
if (
    gateway.version != 4
    or gateway.is_loopback
    or gateway.is_unspecified
    or gateway.is_multicast
    or gateway.is_link_local
    or source.version != 4
    or source.is_loopback
    or source.is_unspecified
    or source.is_multicast
    or source.is_link_local
    or not any(source.subnet_of(private_range) for private_range in private_ranges)
):
    raise SystemExit(1)
PY
  then
    echo 'coding executor capability ingress configuration is invalid' >&2
    exit 1
  fi
fi

iptables -N "$replacement"
if [[ -n "$gateway" ]]; then
  iptables -A "$replacement" -p tcp -s "$source_cidr" -d "${gateway}/32" --dport "$port" -j ACCEPT
  iptables -A "$replacement" -p tcp -d "${gateway}/32" --dport "$port" -j REJECT
else
  iptables -A "$replacement" -p tcp --dport 11438 -j REJECT
fi

iptables -I INPUT 1 -j "$replacement"
while iptables -D INPUT -j "$chain" 2>/dev/null; do :; done
iptables -F "$chain" 2>/dev/null || true
iptables -X "$chain" 2>/dev/null || true
iptables -E "$replacement" "$chain"
