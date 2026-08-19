#!/usr/bin/env bash
# Local Targon screening MVP probe.
#
# Screening on Targon is three disambiguated jobs, none of them nested Docker:
#   1. Kaniko already built the miner archive.
#   2. runtime-probe / live runtime smoke boots that exact image and hits /health.
#   3. source-review-probe reads a clean source tarball in a screener rental.
#
# This script checks a real miner image tar can be published without docker save,
# then optionally runs the live Targon probes once gcloud is authorized.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

usage() {
  cat <<'EOF'
Usage: scripts/targon-screen-mvp.sh export --image-tar PATH
       scripts/targon-screen-mvp.sh live [--source-tar PATH]

export  Normalize a real miner image tar to the portable screened-image contract.
live    Run source-review-probe and runtime-probe (requires gcloud + Targon secret).
EOF
}

command="${1:-}"
if [[ -z "${command}" ]]; then
  usage
  exit 2
fi
shift

case "${command}" in
  export)
    image_tar=""
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --image-tar)
          image_tar="${2:?}"
          shift 2
          ;;
        *)
          usage
          exit 2
          ;;
      esac
    done
    if [[ -z "${image_tar}" || ! -f "${image_tar}" ]]; then
      echo "missing --image-tar PATH" >&2
      exit 2
    fi
    PYTHONPATH="${REPO_ROOT}/workers/screener${PYTHONPATH:+:${PYTHONPATH}}" python3 - \
      "${image_tar}" <<'PY'
import sys
import tempfile
from pathlib import Path

from ditto_screener.gate import BuildGate

image_tar = Path(sys.argv[1])
fd, dest = tempfile.mkstemp(prefix="ditto-screen-mvp-", suffix=".tar")
Path(dest).unlink(missing_ok=True)
portable_path = None
try:
    portable = BuildGate._portable_image_archive(
        str(image_tar), dest, deadline=None
    )
    portable_path = portable.path
    print(f"portable_image_id={portable.image_id}")
    print(f"portable_bytes={Path(portable.path).stat().st_size}")
finally:
    Path(dest).unlink(missing_ok=True)
    if portable_path is not None:
        Path(portable_path).unlink(missing_ok=True)
PY
    ;;
  live)
    "${SCRIPT_DIR}/targon-smoke.sh" source-review-probe
    "${SCRIPT_DIR}/targon-smoke.sh" runtime-probe
    ;;
  *)
    usage
    exit 2
    ;;
esac
