#!/usr/bin/env bash
# Runs on a credential-empty preview VM. PR code is intentionally confined to
# this machine and receives no production or cloud-control credentials.
#
# This is also the startup script for the sn118-preview-base bake VM, and that
# is deliberate: the baked image can only ever contain what a real preview boot
# installs and builds, because it is produced by running this exact path.
set -euo pipefail
exec > >(tee -a /var/log/sn118-preview-startup.log) 2>&1

startup_failed() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # The controller greps the serial console for this exact line, so a boot
    # failure surfaces in about a minute instead of after a 35-minute poll that
    # can only report that the preview never came up. The trap is discarded by
    # the exec at the end of this file; runtime.sh and bake.sh emit the same
    # sentinel.
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

preview_toolchain_present() {
  command -v docker >/dev/null \
    && command -v jq >/dev/null \
    && command -v git >/dev/null \
    && command -v curl >/dev/null \
    && docker compose version >/dev/null 2>&1
}

# On a baked sn118-preview-base image the toolchain is already installed, and
# running the install anyway is not a no-op: apt-get install upgrades a package
# whose candidate has since moved on, which re-downloads dockerd and restarts it
# in the middle of boot. Skipping keeps the point of the bake intact. The loop
# below remains the path for a stock Ubuntu boot, which is still what happens
# whenever GCP_PREVIEW_IMAGE_FAMILY is unset.
if preview_toolchain_present; then
  echo "sn118-preview: preview toolchain already present; skipping apt"
else
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
fi
command -v docker >/dev/null || { echo "sn118-preview: docker is missing after install" >&2; exit 1; }
systemctl enable --now docker

metadata_header='Metadata-Flavor: Google'
metadata_root='http://metadata.google.internal/computeMetadata/v1/instance/attributes'

preview_fetch_source() {
  # Clear first. A baked image already carries the default-branch tree the bake
  # built from, and extracting a PR tarball over it would leave files from a
  # commit this preview is not running.
  rm -rf /opt/sn118-preview
  install -d -m 0755 /opt/sn118-preview
  curl --fail --location --silent --show-error \
    "https://github.com/ditto-assistant/ditto-subnet/archive/${1}.tar.gz" \
    | tar -xz --strip-components=1 -C /opt/sn118-preview
}

# A bake VM warms the Docker state for the sn118-preview-base family and then
# stops so the controller can capture its disk. It takes a default-branch commit
# rather than a PR head, and there is no preview-config, no snapshot URL, no
# lease and no per-preview secret.
bake_sha="$(curl --fail --silent -H "$metadata_header" "$metadata_root/preview-bake-sha" 2>/dev/null || true)"
if [ -n "$bake_sha" ]; then
  [[ "$bake_sha" =~ ^[0-9a-f]{40}$ ]] || { echo "sn118-preview: bake sha is not a commit" >&2; exit 2; }
  preview_fetch_source "$bake_sha"
  cd /opt/sn118-preview
  exec preview/cloud/bake.sh
fi

curl --fail --silent --show-error -H "$metadata_header" "$metadata_root/preview-config" -o /run/preview-config.json
sha="$(jq -r .sha /run/preview-config.json)"
profile="$(jq -r .profile /run/preview-config.json)"
snapshot_url="$(jq -r .snapshot_url /run/preview-config.json)"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
case "$profile" in stack|stack-copy) ;; *) exit 2 ;; esac

preview_fetch_source "$sha"

cd /opt/sn118-preview
export PREVIEW_SHA="$sha"
export PREVIEW_PROFILE="$profile"
export PREVIEW_SNAPSHOT_URL="$snapshot_url"
exec preview/cloud/runtime.sh
