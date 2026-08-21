#!/usr/bin/env bash
# Read-only Targon rental list, state, and logs.
#
# Streams TARGON_API_KEY from GCP Secret Manager into targon_cli. The key is
# never exported, placed in argv, written to disk, or printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
ORCH="${REPO_ROOT}/services/screener-orchestrator"
PROJECT="${TARGON_GCP_PROJECT:-ditto-app-dev}"
SECRET="${TARGON_SECRET_NAME:-TARGON_API_KEY}"
ORG_SLUG="${TARGON_ORG_SLUG:-ditto}"
TIMEOUT_SECONDS="${TARGON_TIMEOUT_SECONDS:-60}"

usage() {
    cat >&2 <<'EOF'
Usage: query_targon.sh <logs|state|list> [args]

Examples:
  query_targon.sh state wrk-xxxxxxxxxxxxxxxx
  query_targon.sh logs wrk-xxxxxxxxxxxxxxxx --tail 400 --include-state
  query_targon.sh list
EOF
}

command="${1:-}"
case "${command}" in
    logs|state|list) ;;
    *)
        usage
        exit 2
        ;;
esac

command -v gcloud >/dev/null 2>&1 || {
    echo "error: gcloud is required" >&2
    exit 127
}
command -v python3 >/dev/null 2>&1 || {
    echo "error: python3 is required" >&2
    exit 127
}

if [[ ! "${TIMEOUT_SECONDS}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    echo "error: TARGON_TIMEOUT_SECONDS must be a positive number" >&2
    exit 2
fi
awk -v value="${TIMEOUT_SECONDS}" 'BEGIN { if (value <= 0 || value > 120) exit 1 }' || {
    echo "error: TARGON_TIMEOUT_SECONDS must be greater than 0 and at most 120" >&2
    exit 2
}

gcloud secrets versions access latest \
    --project="${PROJECT}" \
    --secret="${SECRET}" \
    | PYTHONPATH="${ORCH}" python3 -m screener_capacity.targon_cli \
        --api-key-stdin \
        --org-slug="${ORG_SLUG}" \
        --timeout-seconds="${TIMEOUT_SECONDS}" \
        "$@"
