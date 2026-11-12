#!/usr/bin/env bash
# Power an on-demand screener host off once every worker has idle-exited.
#
# Each worker exits with the idle status (85) after SCREENER_IDLE_EXIT_SECONDS
# without a claim, and RestartPreventExitStatus keeps it down. This runs from a
# timer as root and powers the host off only when nothing still needs it:
#
#   - every ditto-screener-worker@1..N is inactive and last exited 85, and no
#     other worker instance is running;
#   - no worker has an active-lease.json beside its review journal;
#   - the release updater is not running and its lock is free; and
#   - enrollment is not in progress.
#
# A host whose workers never idle-exit (a crash loop, or a steady trickle of
# work) still powers off once it has been up SCREENER_IDLE_MAX_UPTIME_SECONDS
# (default 6h) with no active lease and the updater idle, so it cannot bill
# indefinitely. Shutdown sends SIGTERM, and a worker that claimed in the
# meantime finishes its signed verdict first (TimeoutStopSec); the capacity
# controller wakes the host again when work is waiting.
#
# Any other state keeps the host up and logs one line saying why. The updater
# lock stays held through the poweroff request so a release activation cannot
# start underneath it.
set -euo pipefail

SYSTEMCTL="${SCREENER_IDLE_SYSTEMCTL:-systemctl}"
FLOCK="${SCREENER_IDLE_FLOCK:-flock}"
STATE_DIR="${SCREENER_IDLE_STATE_DIR:-/var/lib/ditto-screener-fleet}"
UPDATE_LOCK="${SCREENER_IDLE_UPDATE_LOCK:-$STATE_DIR/updater/lock}"
WORKER_PROCESSES="${SCREENER_IDLE_WORKER_PROCESSES:-}"
UPTIME_FILE="${SCREENER_IDLE_UPTIME_FILE:-/proc/uptime}"
MAX_UPTIME="${SCREENER_IDLE_MAX_UPTIME_SECONDS:-21600}"
LEASE_GRACE="${SCREENER_IDLE_LEASE_GRACE_SECONDS:-300}"
IDLE_STATUS=85
UPDATER_UNIT=ditto-screener-fleet-auto-update.service
ENROLL_UNIT=ditto-screener-enroll.service

log() { printf 'ditto-screener-idle-shutdown: %s\n' "$*"; }
stay_up() {
  log "staying up: $*"
  exit 0
}

if ! [[ "$WORKER_PROCESSES" =~ ^[1-9][0-9]?$ ]] || [ "$WORKER_PROCESSES" -gt 16 ]; then
  log "staying up: SCREENER_IDLE_WORKER_PROCESSES must be 1..16"
  exit 2
fi

unit_property() {
  "$SYSTEMCTL" show -p "$2" --value "$1" 2>/dev/null || true
}

process_state() {
  case "$1" in
    active|activating|deactivating|reloading|refreshing) return 0 ;;
    *) return 1 ;;
  esac
}

state="$(unit_property "$UPDATER_UNIT" ActiveState)"
if process_state "$state"; then
  stay_up "release updater is $state"
fi

if ! [ -d "$(dirname "$UPDATE_LOCK")" ]; then
  stay_up "release updater state directory is missing"
fi
# Fixed descriptor 9: it stays open, and the lock held, through the final exec.
if ! { exec 9>>"$UPDATE_LOCK"; } 2>/dev/null; then
  stay_up "release updater lock cannot be opened"
fi
if ! "$FLOCK" -n 9; then
  stay_up "release updater holds its lock"
fi

state="$(unit_property "$ENROLL_UNIT" ActiveState)"
if [ "$state" = activating ]; then
  stay_up "enrollment is activating"
fi

# A lease file whose wall-clock lease_deadline (epoch seconds, written by the
# worker) passed more than LEASE_GRACE ago describes no live Platform lease,
# for example after a worker was killed mid-review; it must not keep the host
# up forever. An unreadable deadline is treated as live.
now="$(date +%s)"
for lease in "$STATE_DIR"/workers/*/active-lease.json; do
  [ -e "$lease" ] || continue
  deadline="$(sed -n 's/.*"lease_deadline"[[:space:]]*:[[:space:]]*\([0-9]\{1,\}\).*/\1/p' "$lease" 2>/dev/null | head -n 1 || true)"
  if [ -z "$deadline" ] || [ $((deadline + LEASE_GRACE)) -ge "$now" ]; then
    stay_up "worker $(basename "$(dirname "$lease")") holds an active lease"
  fi
  log "ignoring expired lease file $lease (deadline $deadline)"
done

uptime_seconds="$(cut -d. -f1 "$UPTIME_FILE" 2>/dev/null || true)"
if [[ "$MAX_UPTIME" =~ ^[0-9]+$ ]] && [[ "$uptime_seconds" =~ ^[0-9]+$ ]] \
  && [ "$MAX_UPTIME" -gt 0 ] && [ "$uptime_seconds" -ge "$MAX_UPTIME" ]; then
  log "powering off: up ${uptime_seconds}s (bound ${MAX_UPTIME}s) with no active lease and the updater idle"
  exec "$SYSTEMCTL" poweroff
fi

for index in $(seq 1 "$WORKER_PROCESSES"); do
  unit="ditto-screener-worker@$index.service"
  state="$(unit_property "$unit" ActiveState)"
  case "$state" in
    inactive|failed) ;;
    *) stay_up "worker $index is ${state:-unknown}" ;;
  esac
  code="$(unit_property "$unit" ExecMainCode)"
  status="$(unit_property "$unit" ExecMainStatus)"
  # ExecMainCode 1 is CLD_EXITED: a normal exit rather than a signal.
  if [ "$code" != 1 ] || [ "$status" != "$IDLE_STATUS" ]; then
    stay_up "worker $index last exited with status ${status:-unknown} (code ${code:-unknown}), not an idle exit $IDLE_STATUS"
  fi
done

running="$("$SYSTEMCTL" list-units --plain --no-legend \
  --state=active,activating,deactivating,reloading \
  'ditto-screener-worker@*.service' 2>/dev/null | awk 'NF { print $1 }')" || running=''
if [ -n "$running" ]; then
  stay_up "worker unit still running: $(printf '%s' "$running" | tr '\n' ' ' | sed 's/ $//')"
fi

log "powering off: all $WORKER_PROCESSES workers exited idle ($IDLE_STATUS), no active lease, updater idle"
exec "$SYSTEMCTL" poweroff
