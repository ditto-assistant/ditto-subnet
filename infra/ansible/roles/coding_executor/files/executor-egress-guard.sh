#!/usr/bin/env bash
set -euo pipefail

# RootlessKit/slirp traffic reaches the host OUTPUT chain under this empty
# daemon identity. DNS is the sole permitted link-local operation; private,
# loopback, metadata, and carrier-grade ranges are denied before any future
# coding container can be introduced. A later candidate-execution role adds the
# narrower capability-only proxy policy and is intentionally separate.

executor_user="${CODING_EXECUTOR_USER:-ditto-coding-executor}"
executor_uid="$(id -u "$executor_user")"
# Keep the replacement name safely below the kernel's iptables chain-name
# limit even when a six-digit PID is appended below.
chain="DCE-EXEC-EGRESS"
replacement="${chain}-$$"

iptables -N "$replacement"
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
