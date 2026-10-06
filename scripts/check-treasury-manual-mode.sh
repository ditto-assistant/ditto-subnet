#!/usr/bin/env bash
# Persistent masks make later accidental timer/service activation fail closed.
set -euo pipefail
for unit in sn118-collector@transfer.service sn118-collector@transfer.timer; do
  if [[ "$(systemctl show --property=LoadState --value "$unit")" != masked ]]; then
    printf '%s\n' "Manual mode requires a persistent mask for $unit" >&2
    exit 1
  fi
done
