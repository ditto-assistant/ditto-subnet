#!/usr/bin/env bash
# Build a miner Dockerfile the way production Targon Kaniko does, then prove
# DittoBench would accept the docker-save identity.
#
# Default context is the starter-kit harness. Use --tiny for the fast busybox
# fixture. --live runs the existing Targon Kaniko capability probe (busybox)
# after the local archive contract passes; it never prints the API key.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ORCH_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${ORCH_ROOT}/../.." && pwd)"
STARTER_KIT="${REPO_ROOT}/miners/dittobench-starter-kit"
TINY_CONTEXT="${ORCH_ROOT}/tests/testdata/tiny-miner"

usage() {
  cat <<'EOF'
Usage: scripts/targon-screen-starter-kit.sh [--tiny|--context DIR] [--out-dir DIR] [--live]

Local docker build uses the production Kaniko destination
ditto-screen/{agent}-{attempt}:latest, then validates that the saved tar's
config digest is the scoring identity. --live additionally runs
targon-smoke.sh kaniko-probe.
EOF
}

context="${STARTER_KIT}"
out_dir=""
live=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tiny)
      context="${TINY_CONTEXT}"
      shift
      ;;
    --context)
      context="${2:?}"
      shift 2
      ;;
    --out-dir)
      out_dir="${2:?}"
      shift 2
      ;;
    --live)
      live=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

if [[ ! -f "${context}/Dockerfile" ]]; then
  echo "missing Dockerfile under ${context}" >&2
  exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required for the local Kaniko-equivalent build" >&2
  exit 127
fi

if [[ -z "${out_dir}" ]]; then
  out_dir="$(mktemp -d "${TMPDIR:-/tmp}/ditto-targon-screen.XXXXXX")"
fi
mkdir -p "${out_dir}"

ids_json="$(
  PYTHONPATH="${ORCH_ROOT}" python3 -m screener_capacity.targon_screen_contract ids
)"
printf '%s\n' "${ids_json}"
eval "$(
  PYTHONPATH="${ORCH_ROOT}" python3 - "${ids_json}" <<'PY'
import json, shlex, sys
payload = json.loads(sys.argv[1])
for key in ("agent_id", "attempt_id", "kaniko_destination"):
    print(f"{key}={shlex.quote(payload[key])}")
PY
)"
destination="${kaniko_destination}"

source_tar="${out_dir}/source.tar.gz"
image_tar="${out_dir}/image.tar"
PYTHONPATH="${ORCH_ROOT}" python3 -m screener_capacity.targon_screen_contract pack \
  --context "${context}" --output "${source_tar}"

echo "docker build --tag ${destination} ${context}"
docker build --tag "${destination}" "${context}"
docker save "${destination}" -o "${image_tar}"
docker image rm -f "${destination}" >/dev/null

PYTHONPATH="${ORCH_ROOT}" python3 -m screener_capacity.targon_screen_contract validate \
  --image-tar "${image_tar}" --agent-id "${agent_id}" --attempt-id "${attempt_id}"

if [[ "${live}" -eq 1 ]]; then
  source_sha="$(
    git ls-remote https://github.com/ditto-assistant/dittobench-starter-kit.git HEAD \
      | awk '{print $1}'
  )"
  if [[ "${#source_sha}" -ne 40 ]]; then
    echo "could not resolve dittobench-starter-kit HEAD" >&2
    exit 1
  fi
  "${SCRIPT_DIR}/targon-smoke.sh" kaniko-probe \
    --resource cpu-large \
    --starter-kit-sha "${source_sha}" \
    --provision-timeout-seconds 2400
fi
