#!/usr/bin/env bash
set -euo pipefail

# Run only from a protected release operator environment. The executor host
# never contacts a registry: this verifies the signed immutable image, exports
# an archive, and emits the matching bundle manifest for later IAP transfer.

usage() {
  echo "usage: $0 --release-manifest ABSOLUTE_PATH --output-dir ABSOLUTE_PATH" >&2
  exit 2
}

release_manifest=''
output_dir=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release-manifest) release_manifest="${2:-}"; shift 2 ;;
    --output-dir) output_dir="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done
[[ "$release_manifest" == /* && "$output_dir" == /* ]] || usage
[[ -f "$release_manifest" && ! -L "$release_manifest" && -d "$output_dir" && ! -L "$output_dir" ]] || {
  echo 'release manifest and output directory must be existing non-symlink paths' >&2
  exit 1
}
command -v cosign >/dev/null
command -v docker >/dev/null
command -v sha256sum >/dev/null
command -v python3 >/dev/null

image_reference="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["image_reference"])' "$release_manifest")"
[[ "$image_reference" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || {
  echo 'release manifest image reference is invalid' >&2
  exit 1
}
verified_attestation="$(mktemp)"
trap 'rm -f "$verified_attestation"' EXIT
cosign verify-attestation \
  --output json \
  --type io.heyditto.dittobench.coding-executor-scorer-release.v1 \
  --certificate-identity-regexp '^https://github.com/ditto-assistant/ditto-subnet/.github/workflows/release.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$image_reference" >"$verified_attestation"
python3 - "$release_manifest" "$verified_attestation" <<'PY'
import base64
import json
import sys
from pathlib import Path

release = Path(sys.argv[1]).read_bytes()
records = json.loads(Path(sys.argv[2]).read_bytes())
if isinstance(records, dict):
    records = [records]
if not isinstance(records, list):
    raise SystemExit("verified scorer attestation output is invalid")
for record in records:
    if not isinstance(record, dict) or not isinstance(record.get("payload"), str):
        continue
    try:
        statement = json.loads(base64.b64decode(record["payload"], validate=True))
        predicate = statement["predicate"]
        canonical = (json.dumps(predicate, sort_keys=True, separators=(",", ":")) + "\n").encode()
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        continue
    if statement.get("predicateType") == "io.heyditto.dittobench.coding-executor-scorer-release.v1" and canonical == release:
        break
else:
    raise SystemExit("release manifest is not the exact verified scorer attestation predicate")
PY
docker pull "$image_reference" >/dev/null
archive="$output_dir/coding-executor-scorer.oci.tar"
bundle_manifest="$output_dir/coding-executor-scorer.bundle.json"
[[ ! -e "$archive" && ! -e "$bundle_manifest" ]] || {
  echo 'refusing to overwrite an existing scorer bundle output' >&2
  exit 1
}
docker image save --output "$archive" "$image_reference"
archive_sha256="$(sha256sum "$archive" | awk '{print $1}')"
[[ "$archive_sha256" =~ ^[0-9a-f]{64}$ ]]
python3 scripts/render-coding-executor-scorer-bundle.py \
  --release-manifest "$release_manifest" \
  --archive-sha256 "$archive_sha256" \
  --output "$bundle_manifest"
chmod 0600 "$archive" "$bundle_manifest"
printf '%s\n' "$archive_sha256"
