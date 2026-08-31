#!/usr/bin/env bash
set -euo pipefail

# Copy a release-provided manifest/archive pair into the fixed root-owned
# staging directory, verify it before publication, and never contact a
# registry or Docker. An operator transfers source files through the protected
# IAP/release path; this host has no registry or secret credentials.

staging_dir='/var/lib/ditto-coding-executor/staged'
verify='/usr/local/lib/ditto-coding-executor/verify-runtime-bundle.py'

usage() {
  echo "usage: $0 --manifest-source ABSOLUTE_PATH --archive-source ABSOLUTE_PATH --expected-manifest-sha256 SHA256" >&2
  exit 2
}

manifest_source=''
archive_source=''
expected_manifest_sha256=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --manifest-source)
      manifest_source="${2:-}"
      shift 2
      ;;
    --archive-source)
      archive_source="${2:-}"
      shift 2
      ;;
    --expected-manifest-sha256)
      expected_manifest_sha256="${2:-}"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo 'stage-runtime-bundle.sh must run as root' >&2
  exit 1
fi
if [[ ! "$manifest_source" =~ ^/ || ! "$archive_source" =~ ^/ ]]; then
  echo 'runtime-bundle sources must be absolute paths' >&2
  exit 1
fi
if [[ ! "$expected_manifest_sha256" =~ ^[0-9a-f]{64}$ ]]; then
  echo 'expected manifest SHA-256 must be lowercase hexadecimal' >&2
  exit 1
fi
if [[ ! -f "$manifest_source" || -L "$manifest_source" || ! -f "$archive_source" || -L "$archive_source" ]]; then
  echo 'runtime-bundle sources must be regular non-symlink files' >&2
  exit 1
fi
if (( $(stat -c '%s' "$manifest_source") == 0 || $(stat -c '%s' "$manifest_source") > 32768 )); then
  echo 'runtime-bundle manifest source size is outside its bound' >&2
  exit 1
fi
if (( $(stat -c '%s' "$archive_source") == 0 || $(stat -c '%s' "$archive_source") > 8589934592 )); then
  echo 'runtime-bundle archive source size is outside its bound' >&2
  exit 1
fi
if [[ ! -x "$verify" ]]; then
  echo 'runtime-bundle verifier is not installed' >&2
  exit 1
fi

install -d -o root -g root -m 0700 "$staging_dir"
temporary_dir="$(mktemp -d "$staging_dir/.incoming.XXXXXXXX")"
install -o root -g root -m 0600 "$manifest_source" "$temporary_dir/runtime-manifest.json"
install -o root -g root -m 0600 "$archive_source" "$temporary_dir/supervisor.oci.tar"

"$verify" \
  --manifest "$temporary_dir/runtime-manifest.json" \
  --archive "$temporary_dir/supervisor.oci.tar" \
  --expected-manifest-sha256 "$expected_manifest_sha256"

mv -f "$temporary_dir/runtime-manifest.json" "$staging_dir/runtime-manifest.json"
mv -f "$temporary_dir/supervisor.oci.tar" "$staging_dir/supervisor.oci.tar"
rmdir "$temporary_dir"
printf '%s\n' 'runtime bundle staged and verified; Docker was not contacted'
