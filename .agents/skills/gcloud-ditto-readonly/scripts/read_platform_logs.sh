#!/usr/bin/env bash
# Bounded, read-only reads of the live Platform pm2 logs on the production VM.
#
# The live tree is /opt/ditto-subnet/apps/platform/logs (pm2 cwd is the
# monorepo checkout). /opt/ditto-platform/logs is the pre-cutover tree and is
# stale. ditto-api.err.log is empty by design: uvicorn access lines, app
# logging, AND unhandled-exception tracebacks all land in ditto-api.out.log.
set -euo pipefail

readonly PROJECT="ditto-app-dev"
readonly ZONE="us-central1-a"
readonly INSTANCE="ditto-platform-prod"
readonly LOG_DIR="/opt/ditto-subnet/apps/platform/logs"
readonly MAX_LINES=400
readonly MAX_CONTEXT=10

usage() {
    cat >&2 <<'EOF'
Usage:
  read_platform_logs.sh tail [lines]
  read_platform_logs.sh grep <extended-regex> [context-lines] [max-lines]
  read_platform_logs.sh --file <api|relay-1|relay-2|image-cleanup> <mode> ...

Examples:
  read_platform_logs.sh tail 200
  read_platform_logs.sh grep 'submission-source-reviews/.*complete' 3 120
  read_platform_logs.sh grep 'unhandled exception' 40 120
  read_platform_logs.sh --file image-cleanup tail 50

Reads only; output is bounded. The file is multi-GB, so grep matches are
taken from the END of the log (most recent first in wall-clock terms).
EOF
}

command -v gcloud >/dev/null 2>&1 || { echo "error: gcloud is required" >&2; exit 127; }

log_file="ditto-api.out.log"
if [[ "${1:-}" == "--file" ]]; then
    case "${2:-}" in
        api) log_file="ditto-api.out.log" ;;
        relay-1) log_file="ditto-api-relay-1.out.log" ;;
        relay-2) log_file="ditto-api-relay-2.out.log" ;;
        image-cleanup) log_file="ditto-image-cleanup.err.log" ;;
        *) usage; exit 2 ;;
    esac
    shift 2
fi

mode="${1:-}"
case "$mode" in
tail)
    lines="${2:-200}"
    [[ "$lines" =~ ^[0-9]+$ ]] && (( lines >= 1 && lines <= 2000 )) || {
        echo "error: tail lines must be 1-2000" >&2; exit 2; }
    remote_command="sudo -n -u deploy tail -n $lines $LOG_DIR/$log_file"
    ;;
grep)
    pattern="${2:-}"
    [[ -n "$pattern" ]] || { usage; exit 2; }
    if [[ "$pattern" == *$'\n'* ]]; then
        echo "error: pattern must be a single line" >&2; exit 2
    fi
    context="${3:-2}"
    max_lines="${4:-$MAX_LINES}"
    [[ "$context" =~ ^[0-9]+$ ]] && (( context <= MAX_CONTEXT )) || {
        echo "error: context lines must be 0-$MAX_CONTEXT" >&2; exit 2; }
    [[ "$max_lines" =~ ^[0-9]+$ ]] && (( max_lines >= 1 && max_lines <= MAX_LINES )) || {
        echo "error: max lines must be 1-$MAX_LINES" >&2; exit 2; }
    printf -v quoted_pattern '%q' "$pattern"
    # Search only the trailing slice: the log is multi-GB and unrotated, and
    # live diagnosis wants the newest occurrences anyway.
    remote_command="sudo -n -u deploy bash -c 'tail -c 500000000 $LOG_DIR/$log_file | grep -aE -C $context -- $quoted_pattern | tail -n $max_lines'"
    ;;
*)
    usage; exit 2 ;;
esac

exec gcloud compute ssh "$INSTANCE" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="$remote_command"
