#!/usr/bin/env bash
# Runs on a credential-empty preview VM. PR code is intentionally confined to
# this machine and receives no production or cloud-control credentials.
set -euo pipefail
exec > >(tee -a /var/log/sn118-preview-startup.log) 2>&1

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl git jq docker.io docker-compose-v2
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
