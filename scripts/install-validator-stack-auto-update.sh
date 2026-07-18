#!/usr/bin/env bash
# Install the opt-in complete-stack updater. First adoption remains supervised.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=/etc/systemd/system/ditto-validator-stack-auto-update.service
TIMER=/etc/systemd/system/ditto-validator-stack-auto-update.timer
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

require_systemd_path() {
  case "$2" in
    /*) ;;
    *) die "$1 must be an absolute path" ;;
  esac
  case "$2" in
    *$'\n'* | *$'\r'* | *'"'* | *\\* | *'%'*)
      die "$1 contains characters that cannot be persisted safely"
      ;;
  esac
}

[ "$(id -u)" -eq 0 ] || die "run with sudo"
service_path=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
PATH="$service_path" command -v cosign >/dev/null 2>&1 || \
  die "install the pinned cosign verifier in the system PATH before enabling stack updates"
[ -f "$ROOT_DIR/.env" ] || die "create $ROOT_DIR/.env first"
grep -Eq '^VALIDATOR_STACK_AUTO_UPDATE=(true|1|yes)$' "$ROOT_DIR/.env" || die "set VALIDATOR_STACK_AUTO_UPDATE=true only after supervised stack adoption"
if grep -Eq '^VALIDATOR_AUTO_UPDATE=(true|1|yes)$' "$ROOT_DIR/.env"; then
  die "disable the legacy validator-only updater before enabling full-stack updates"
fi
service_user="${DITTO_VALIDATOR_UPDATE_USER:-${SUDO_USER:-}}"
[ -n "$service_user" ] && [ "$service_user" != root ] || die "set DITTO_VALIDATOR_UPDATE_USER to the non-root Docker operator"
id "$service_user" >/dev/null 2>&1 || die "operator user does not exist: $service_user"
service_group="$(id -gn "$service_user")"; service_home="$(getent passwd "$service_user" | awk -F: 'NR==1 {print $6}')"
[ -n "$service_home" ] || die "could not determine operator home"
docker_config="${DITTO_VALIDATOR_DOCKER_CONFIG:-$service_home/.docker}"
wallets_dir="${DITTO_BITTENSOR_WALLETS_DIR:-}"
if [ -z "$wallets_dir" ]; then
  wallets_dir="$(awk -F= '$1 == "DITTO_BITTENSOR_WALLETS_DIR" { print substr($0, index($0, "=") + 1) }' "$ROOT_DIR/.env" | tail -1)"
fi
wallets_dir="${wallets_dir:-$service_home/.bittensor/wallets}"
require_systemd_path HOME "$service_home"
require_systemd_path DOCKER_CONFIG "$docker_config"
require_systemd_path DITTO_BITTENSOR_WALLETS_DIR "$wallets_dir"
[ -d "$wallets_dir" ] || die "wallet directory does not exist: $wallets_dir"
state_dir="$ROOT_DIR/.validator-stack-update"
[ -f "$state_dir/managed-release.env" ] || die "run validator-stack-auto-update.sh adopt as the operator before installing"
install -d -m 0700 -o "$service_user" -g "$service_group" "$state_dir"
[ -z "$(find "$state_dir" -type l -print -quit)" ] || die "remove symbolic links from $state_dir"
chown -R "$service_user:$service_group" "$state_dir"; find "$state_dir" -type d -exec chmod 0700 {} +; find "$state_dir" -type f -exec chmod 0600 {} +

cat >"$SERVICE" <<EOF
[Unit]
Description=Transactionally update the complete Ditto SN118 validator stack
After=docker.service network-online.target
Wants=network-online.target
Requires=docker.service

[Service]
Type=oneshot
User=$service_user
WorkingDirectory=$ROOT_DIR
Environment="HOME=$service_home"
Environment="PATH=$service_path"
Environment="DOCKER_CONFIG=$docker_config"
Environment="DITTO_BITTENSOR_WALLETS_DIR=$wallets_dir"
Environment=DITTO_SUBNET_ENV_FILE=$ROOT_DIR/.env
Environment=DITTO_VALIDATOR_STACK_UPDATE_STATE_DIR=$state_dir
ExecStart=$ROOT_DIR/scripts/validator-stack-auto-update.sh run
TimeoutStartSec=10800
TimeoutStopSec=600
UMask=0077
NoNewPrivileges=true
CapabilityBoundingSet=
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$state_dir
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
SystemCallArchitectures=native
EOF

cat >"$TIMER" <<'EOF'
[Unit]
Description=Poll for compatible complete Ditto validator-stack releases

[Timer]
OnBootSec=15m
OnUnitInactiveSec=15m
RandomizedDelaySec=5m
Persistent=true
Unit=ditto-validator-stack-auto-update.service

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now ditto-validator-stack-auto-update.timer
printf 'installed complete-stack updater for %s\n' "$service_user"
