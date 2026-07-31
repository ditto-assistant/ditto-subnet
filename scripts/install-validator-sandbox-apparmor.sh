#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT_DIR/config/apparmor/ditto-rootless-dind"
DESTINATION="/etc/apparmor.d/ditto-rootless-dind"
ENV_FILE="${DITTO_SUBNET_ENV_FILE:-$ROOT_DIR/.env}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run this installer as root"
restriction="$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || true)"
if [ "$restriction" != "1" ]; then
  printf 'restricted unprivileged user namespaces are disabled; no Ditto AppArmor exception is required\n'
  exit 0
fi
command -v apparmor_parser >/dev/null 2>&1 || die "apparmor_parser is required (install apparmor-utils)"
[ -f "$SOURCE" ] && [ ! -L "$SOURCE" ] || die "AppArmor profile source is unavailable"
[ ! -L "$DESTINATION" ] || die "$DESTINATION must not be a symbolic link"

install -o root -g root -m 0644 "$SOURCE" "$DESTINATION"
apparmor_parser -r -W "$DESTINATION"

if [ -r /sys/kernel/security/apparmor/profiles ] && ! grep -q '^ditto-rootless-dind ' /sys/kernel/security/apparmor/profiles; then
  die "ditto-rootless-dind did not load"
fi
[ -f "$ENV_FILE" ] || die "create $ENV_FILE before installing the validator AppArmor profile"
if grep -q '^DITTO_SANDBOX_APPARMOR_PROFILE=' "$ENV_FILE"; then
  grep -q '^DITTO_SANDBOX_APPARMOR_PROFILE=ditto-rootless-dind$' "$ENV_FILE" || \
    die "remove the conflicting DITTO_SANDBOX_APPARMOR_PROFILE setting from $ENV_FILE"
else
  printf '\nDITTO_SANDBOX_APPARMOR_PROFILE=ditto-rootless-dind\n' >>"$ENV_FILE"
fi
printf 'installed and loaded AppArmor profile ditto-rootless-dind\n'
