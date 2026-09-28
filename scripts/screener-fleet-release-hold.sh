#!/usr/bin/env bash
# Operator gate for a review that outlived its lease and stopped progressing.
# A fresh review is refused. This command does not send SIGKILL.
set -euo pipefail

STATE_DIR="${SCREENER_FLEET_UPDATE_STATE_DIR:-/var/lib/ditto-screener-fleet/updater}"
FLEET_STATE_DIR="${SCREENER_FLEET_STATE_DIR:-$(dirname "$STATE_DIR")}"
SYSTEMCTL="${SCREENER_FLEET_SYSTEMCTL:-systemctl}"
PYTHON="${SCREENER_FLEET_PYTHON:-python3}"
if [ -n "${SCREENER_FLEET_DRAIN_PY:-}" ]; then
  DRAIN_PY="$SCREENER_FLEET_DRAIN_PY"
elif [ -f "$(dirname "$0")/screener-fleet-drain.py" ]; then
  DRAIN_PY="$(dirname "$0")/screener-fleet-drain.py"
else
  DRAIN_PY="$STATE_DIR/screener-fleet-drain.py"
fi
CONFIRMATION="RELEASE STUCK SCREENER REVIEW"

worker="${1:-}"
phrase="${2:-}"
[[ "$worker" =~ ^[1-9][0-9]*$ ]] || {
  echo "usage: $0 <worker-index> '$CONFIRMATION'" >&2
  exit 2
}
[ "$phrase" = "$CONFIRMATION" ] || {
  echo "refusing: confirmation phrase does not match" >&2
  exit 2
}

lease="$FLEET_STATE_DIR/workers/$worker/active-lease.json"
decision="$("$PYTHON" "$DRAIN_PY" --lease "$lease" --now "$(date +%s)")"
case "$decision" in
  wait|ready)
    echo "refusing: worker $worker is idle or still inside a progressing lease" >&2
    exit 1
    ;;
  held)
    ;;
  *)
    echo "refusing: unknown drain decision" >&2
    exit 1
    ;;
esac

install -d -m 0700 "$STATE_DIR"
umask 077
printf 'WORKER=%s\nDECIDED_AT=%s\nACTION=operator-hold\n' \
  "$worker" "$(date +%s)" >"$STATE_DIR/operator-hold-worker-$worker.env"
# Ask the unit to finish. Do not escalate. If the process is already gone,
# Platform parks the unsigned attempt for manual retry after the heartbeat grace.
"$SYSTEMCTL" kill --kill-whom=main -s SIGTERM \
  "ditto-screener-worker@${worker}.service" || true
echo "recorded operator hold for worker $worker; no SIGKILL was sent"
