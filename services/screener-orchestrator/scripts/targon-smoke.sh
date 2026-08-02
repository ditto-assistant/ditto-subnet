#!/usr/bin/env bash
# Secret-safe Targon inventory and Rental lifecycle smoke commands.
#
# The authenticated path streams the key from GCP Secret Manager into the
# Python process. It is never exported, placed in argv, or printed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT="${TARGON_GCP_PROJECT:-ditto-app-dev}"
SECRET="${TARGON_SECRET_NAME:-TARGON_API_KEY}"

usage() {
  cat <<'EOF'
Usage: scripts/targon-smoke.sh <inventory|list|rootless-probe|buildkit-probe|dind-probe|kaniko-probe> [options]

Authenticated commands read TARGON_API_KEY from GCP Secret Manager without
placing it in the shell environment, command line, or output.
EOF
}

command="${1:-}"
if [[ -z "${command}" ]]; then
  usage
  exit 2
fi
shift

case "${command}" in
  inventory)
    PYTHONPATH="${REPO_ROOT}" python3 -m screener_capacity.targon_cli \
      inventory "$@"
    ;;
  list|rootless-probe|buildkit-probe|dind-probe|kaniko-probe)
    gcloud secrets versions access latest \
      --project="${PROJECT}" \
      --secret="${SECRET}" \
      | PYTHONPATH="${REPO_ROOT}" python3 -m screener_capacity.targon_cli \
          --api-key-stdin "${command}" "$@"
    ;;
  *)
    usage
    exit 2
    ;;
esac
