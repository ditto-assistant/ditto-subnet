#!/usr/bin/env bash
# Persistent masks make later accidental timer/service activation fail closed.
set -euo pipefail
for unit in sn118-collector@transfer.service sn118-collector@transfer.timer; do
  enablement=$(systemctl is-enabled "$unit" 2>/dev/null || :)
  active=$(systemctl show --property=ActiveState --value "$unit" 2>/dev/null || :)
  if [[ "$(systemctl show --property=LoadState --value "$unit")" != masked || "$enablement" != masked || "$active" != inactive ]]; then
    printf '%s\n' "Manual mode requires an inactive, persistently masked $unit" >&2
    exit 1
  fi
done
