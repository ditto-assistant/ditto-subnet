#!/usr/bin/env bash

configure_validator_sandbox_apparmor() {
  local restriction profiles
  restriction="$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || true)"
  if [ "$restriction" != "1" ]; then
    export DITTO_SANDBOX_APPARMOR_PROFILE="${DITTO_SANDBOX_APPARMOR_PROFILE:-docker-default}"
    return 0
  fi

  profiles="${DITTO_APPARMOR_PROFILES_PATH:-/sys/kernel/security/apparmor/profiles}"
  # Unprivileged systemd services cannot read the kernel profile list on some
  # hardened hosts.  When it is readable, keep the early diagnostic; when it
  # is not, select the required profile and let the Docker daemon enforce that
  # it is actually loaded when the sandbox container is created.
  if [ -r "$profiles" ]; then
    if grep -q '^ditto-rootless-dind ' "$profiles" 2>/dev/null; then
      :
    elif [ "$?" -eq 1 ]; then
      printf '%s\n' \
        'error: restricted unprivileged user namespaces require the ditto-rootless-dind AppArmor profile' \
        'run: sudo ./scripts/install-validator-sandbox-apparmor.sh' >&2
      return 1
    fi
  fi
  export DITTO_SANDBOX_APPARMOR_PROFILE=ditto-rootless-dind
}
