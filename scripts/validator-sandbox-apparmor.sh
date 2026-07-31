#!/usr/bin/env bash

configure_validator_sandbox_apparmor() {
  local restriction profiles
  restriction="$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || true)"
  if [ "$restriction" != "1" ]; then
    export DITTO_SANDBOX_APPARMOR_PROFILE="${DITTO_SANDBOX_APPARMOR_PROFILE:-docker-default}"
    return 0
  fi

  profiles="${DITTO_APPARMOR_PROFILES_PATH:-/sys/kernel/security/apparmor/profiles}"
  if [ ! -r "$profiles" ] || ! grep -q '^ditto-rootless-dind ' "$profiles"; then
    printf '%s\n' \
      'error: restricted unprivileged user namespaces require the ditto-rootless-dind AppArmor profile' \
      'run: sudo ./scripts/install-validator-sandbox-apparmor.sh' >&2
    return 1
  fi
  export DITTO_SANDBOX_APPARMOR_PROFILE=ditto-rootless-dind
}
