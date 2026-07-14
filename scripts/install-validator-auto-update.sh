#!/usr/bin/env bash
# Install the opt-in validator updater as a short-lived, jittered systemd timer.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=/etc/systemd/system/ditto-validator-auto-update.service
TIMER=/etc/systemd/system/ditto-validator-auto-update.timer

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

[ "$(id -u)" -eq 0 ] || die "run with sudo"
[ -f "$ROOT_DIR/.env" ] || die "create $ROOT_DIR/.env first"
grep -Eq '^VALIDATOR_AUTO_UPDATE=(true|1|yes)$' "$ROOT_DIR/.env" || \
  die "set VALIDATOR_AUTO_UPDATE=true in .env before installing"

service_user="${DITTO_VALIDATOR_UPDATE_USER:-${SUDO_USER:-}}"
[ -n "$service_user" ] && [ "$service_user" != root ] || \
  die "set DITTO_VALIDATOR_UPDATE_USER to the non-root operator who can run Docker"
id "$service_user" >/dev/null 2>&1 || die "operator user does not exist: $service_user"

timeout_budget="$("$ROOT_DIR/scripts/validator-auto-update.sh" budget)"
start_timeout_seconds="$(
  awk -F= '$1 == "TIMEOUT_START_SECONDS" { print $2 }' <<<"$timeout_budget"
)"
stop_timeout_seconds="$(
  awk -F= '$1 == "TIMEOUT_STOP_SECONDS" { print $2 }' <<<"$timeout_budget"
)"
[[ "$start_timeout_seconds" =~ ^[1-9][0-9]*$ ]] || die "invalid updater start timeout budget"
[[ "$stop_timeout_seconds" =~ ^[1-9][0-9]*$ ]] || die "invalid updater stop timeout budget"

mkdir -p "$ROOT_DIR/.validator-update"
chown "$service_user":"$(id -gn "$service_user")" "$ROOT_DIR/.validator-update"

cat >"$SERVICE" <<EOF
[Unit]
Description=Safely update the labelled Ditto SN118 validator
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
User=$service_user
WorkingDirectory=$ROOT_DIR
Environment=DITTO_SUBNET_BUILD_CACHE=$ROOT_DIR/.validator-update/build-cache
ExecStart=$ROOT_DIR/scripts/validator-auto-update.sh run
TimeoutStartSec=${start_timeout_seconds}s
TimeoutStopSec=${stop_timeout_seconds}s
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$ROOT_DIR/.validator-update
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
EOF

cat >"$TIMER" <<'EOF'
[Unit]
Description=Poll for compatible Ditto SN118 validator releases

[Timer]
OnBootSec=15m
OnUnitInactiveSec=15m
RandomizedDelaySec=5m
Persistent=true
Unit=ditto-validator-auto-update.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now ditto-validator-auto-update.timer
printf 'installed for user %s; first check is due within 20 minutes\n' "$service_user"
