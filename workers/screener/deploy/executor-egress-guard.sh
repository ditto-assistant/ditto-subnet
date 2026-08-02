#!/usr/bin/env bash
set -euo pipefail

# RootlessKit/slirp traffic appears in the host OUTPUT path as ditto-builder.
# Builds may reach public package registries, but never loopback, metadata, the
# VPC, peered private networks, or carrier-grade NAT.
executor_user="${SCREENER_EXECUTOR_USER:-ditto-builder}"
executor_uid="$(id -u "$executor_user")"
chain="DITTO-EXEC-EGRESS"
replacement="${chain}-$$"

iptables -N "$replacement"
# GCE serves DNS on the metadata IP. DNS is the only permitted link-local use.
iptables -A "$replacement" -p udp -d 169.254.169.254/32 --dport 53 -j ACCEPT
iptables -A "$replacement" -p tcp -d 169.254.169.254/32 --dport 53 -j ACCEPT
for cidr in \
  10.0.0.0/8 \
  100.64.0.0/10 \
  127.0.0.0/8 \
  169.254.0.0/16 \
  172.16.0.0/12 \
  192.168.0.0/16; do
  iptables -A "$replacement" -d "$cidr" -j REJECT
done

iptables -I OUTPUT 1 -m owner --uid-owner "$executor_uid" -j "$replacement"
while iptables -D OUTPUT -m owner --uid-owner "$executor_uid" -j "$chain" 2>/dev/null; do :; done
iptables -F "$chain" 2>/dev/null || true
iptables -X "$chain" 2>/dev/null || true
iptables -E "$replacement" "$chain"
