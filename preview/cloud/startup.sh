#!/usr/bin/env bash
# Runs on a credential-empty preview VM. PR code is intentionally confined to
# this machine and receives no production or cloud-control credentials.
set -euo pipefail
exec > >(tee -a /var/log/sn118-preview-startup.log) 2>&1

startup_failed() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # The controller greps the serial console for this exact line, so a boot
    # failure surfaces in about a minute instead of after a 35-minute poll that
    # can only report that the preview never came up. The trap is discarded by
    # the exec at the end of this file; runtime.sh emits the same sentinel.
    echo "sn118-preview: startup failed with status ${status}" >&2
  fi
}
trap startup_failed EXIT

# This subnet is IPv4-only, but apt was resolving and dialling AAAA records for
# us-central1.gce.archive.ubuntu.com. Together with an IPv4 timeout to
# security.ubuntu.com that degraded the index refresh, the install then reported
# "no installation candidate" and set -e killed this script two minutes into
# boot -- while the controller kept polling for another 33.
printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "3";\n' \
  >/etc/apt/apt.conf.d/99-sn118-preview

apt_bootstrap() {
  # A degraded refresh is not fatal on its own; the install below is the real
  # gate, and a failed install re-enters this function with a cleared index.
  apt-get update || echo "sn118-preview: apt-get update was incomplete" >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git jq docker.io docker-compose-v2
}

delay=5
for attempt in 1 2 3 4 5; do
  if apt_bootstrap; then
    break
  fi
  if [ "$attempt" -eq 5 ]; then
    echo "sn118-preview: apt could not install the preview toolchain" >&2
    exit 1
  fi
  echo "sn118-preview: apt attempt ${attempt} failed; retrying in ${delay}s" >&2
  rm -rf /var/lib/apt/lists/*
  sleep "$delay"
  delay=$(( delay * 2 ))
done
command -v docker >/dev/null || { echo "sn118-preview: docker is missing after install" >&2; exit 1; }
systemctl enable --now docker

metadata_header='Metadata-Flavor: Google'
metadata_root='http://metadata.google.internal/computeMetadata/v1/instance/attributes'
curl --fail --silent --show-error -H "$metadata_header" "$metadata_root/preview-config" -o /run/preview-config.json
sha="$(jq -r .sha /run/preview-config.json)"
profile="$(jq -r .profile /run/preview-config.json)"
snapshot_url="$(jq -r .snapshot_url /run/preview-config.json)"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
case "$profile" in stack|stack-copy) ;; *) exit 2 ;; esac

install -d -m 0755 /opt/sn118-preview
curl --fail --location --silent --show-error \
  "https://github.com/ditto-assistant/ditto-subnet/archive/${sha}.tar.gz" \
  | tar -xz --strip-components=1 -C /opt/sn118-preview

cd /opt/sn118-preview
export PREVIEW_SHA="$sha"
export PREVIEW_PROFILE="$profile"
export PREVIEW_SNAPSHOT_URL="$snapshot_url"
exec preview/cloud/runtime.sh
