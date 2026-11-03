#!/usr/bin/env bash
# Read-only, bounded Hetzner screener journals; no cloud or operator login.
set -euo pipefail
skill_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
usage() {
  cat <<'HELP'
Usage: read_screener_logs.sh [--worker N] [--service worker|agent|updater|enroll|egress]
       [--since DURATION] [--lines N] [--json] [--follow]
Default: all worker journals, last hour, newest 200 records.
DURATION is a positive number plus s/m/h/d (for example 15m or -1h).
--worker selects one positive worker number; --lines accepts 1..10000.
Uses logs@100.79.84.29 on the omniaura.ai tailnet with committed host-key pins.
Set DITTOBOX_SSH_IDENTITY to one readable approved key (default ~/.ssh/id_ed25519).
For production queue/policy/attempt state, use Backroom first; journals support it.
HELP
}
fail() { echo "$*" >&2; exit 2; }
service=worker
worker=''
since=1h
lines=200
format=short-iso-precise
follow=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --worker) [ "$#" -ge 2 ] || fail '--worker needs a value'; worker="$2"; shift 2 ;;
    --service) [ "$#" -ge 2 ] || fail '--service needs a value'; service="$2"; shift 2 ;;
    --since) [ "$#" -ge 2 ] || fail '--since needs a value'; since="$2"; shift 2 ;;
    --lines|-n) [ "$#" -ge 2 ] || fail '--lines needs a value'; lines="$2"; shift 2 ;;
    --json) format=json; shift ;;
    --follow|-f) follow=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) fail "Unknown argument: $1" ;;
  esac
done
[[ "$lines" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$lines" -le 10000 ] || fail '--lines must be 1..10000'
[[ "$since" =~ ^-?([1-9][0-9]{0,5})([smhd])$ ]] || fail '--since expects a positive duration such as 15m or -1h'
amount="${BASH_REMATCH[1]}"
case "${BASH_REMATCH[2]}" in
  s) seconds="$amount" ;; m) seconds=$((amount * 60)) ;;
  h) seconds=$((amount * 3600)) ;; d) seconds=$((amount * 86400)) ;;
esac
case "$service" in
  worker) unit='ditto-screener-worker@*.service' ;;
  agent) unit=ditto-screener-fleet-agent.service ;;
  updater) unit=ditto-screener-fleet-auto-update.service ;;
  enroll) unit=ditto-screener-enroll.service ;;
  egress) unit=ditto-screener-egress-guard.service ;;
  *) fail '--service expects worker, agent, updater, enroll or egress' ;;
esac
if [ -n "$worker" ]; then
  [[ "$worker" =~ ^[1-9][0-9]{0,4}$ ]] || fail '--worker expects a positive worker number'
  [ "$service" = worker ] || fail '--worker only applies to the worker service'
  unit="ditto-screener-worker@${worker}.service"
fi
[ "${DITTOBOX_LOGS_USER:-logs}" = logs ] || fail 'Log reads require the logs account; operator usernames are not a fallback.'
identity="${DITTOBOX_SSH_IDENTITY-$HOME/.ssh/id_ed25519}"
[ -n "$identity" ] && [ -f "$identity" ] && [ -r "$identity" ] || fail 'Set DITTOBOX_SSH_IDENTITY to one readable approved SSH key; no automatic key fallback.'
args=(journalctl --no-pager --unit "$unit" --since "$seconds seconds ago" --lines "$lines" --output "$format")
if [ "$follow" = true ]; then args+=(--follow); fi
printf -v remote_command '%q ' "${args[@]}"
printf '# host=subnet-screener-1 user=logs source=journal unit=%s since=%s lines=%s\n' "$unit" "$since" "$lines" >&2
result=0
command ssh -F /dev/null -o BatchMode=yes -o ConnectTimeout=10 \
  -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$skill_dir/screener_known_hosts" \
  -o IdentitiesOnly=yes -i "$identity" logs@100.79.84.29 "$remote_command" || result=$?
if [ "$result" -eq 255 ]; then
  echo 'Screener SSH failed: check the omniaura.ai tailnet, ACL, logs account and approved key. No public-host or privileged-user fallback is attempted.' >&2
fi
exit "$result"
