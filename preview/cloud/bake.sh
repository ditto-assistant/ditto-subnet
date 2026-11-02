#!/usr/bin/env bash
# Guest side of the sn118-preview-base image bake. Reached only from
# startup.sh's preview-bake-sha branch, on a VM with no service account, so this
# has no more authority than a preview VM does -- it just warms Docker state and
# stops.
#
# The point is everything a preview boot would otherwise do over the network on
# a cold machine: apt (already done by startup.sh above), the four pulled compose
# images, and the expensive base layers of the five built ones. BuildKit keeps
# its cache under /var/lib/docker/buildkit on the boot disk, so imaging this disk
# carries the cache with it.
set -euo pipefail
cd "$(dirname "$0")/../.."

bake_failed() {
  status=$?
  if [ "$status" -ne 0 ]; then
    # startup.sh's trap is gone by the time this file runs. bake-image.sh greps
    # the serial console for this exact line, the same way provision.sh does for
    # a preview boot.
    echo "sn118-preview: startup failed with status ${status}" >&2
  fi
  exit "$status"
}
trap bake_failed EXIT

compose() { docker compose -f preview/cloud/compose.yml "$@"; }

# compose.yml interpolates these into service environments. None of them reaches
# a built layer -- the one build arg that does is dashboard's VITE_API_BASE_URL
# below -- but leaving them unset makes compose warn on every invocation and
# makes a genuine warning easy to miss. They are placeholders, not secrets: no
# container is ever started here.
export PREVIEW_SHA="0000000000000000000000000000000000000000"
export PREVIEW_PROFILE="stack"
export PREVIEW_BASE_DOMAIN="bake.invalid"
export PREVIEW_ADMIN_TOKEN="bake"
export PREVIEW_SESSION_SECRET="bake"
export PREVIEW_POSTGRES_PASSWORD="bake"

# Pulled images are the clean win: identical bytes on every preview, pinned by
# tag in compose.yml, and worth ~1.5GB of boot-path download.
compose pull --quiet postgres minio minio-init gateway

# dashboard's VITE_API_BASE_URL is derived from the VM's own IP, so its final
# layer is guaranteed to miss at runtime. Everything under it -- the base image,
# the package manager install, the dependency layers -- still hits, which is the
# part that costs minutes.
compose build

# Build leftovers would otherwise be captured into the image. The build cache
# itself is deliberately kept.
docker image prune --force >/dev/null

echo "sn118-preview: bake images present"
docker image ls --format '{{.Repository}}:{{.Tag}} {{.Size}}'
df -h /var/lib/docker || true

# The controller waits for this exact line before it stops the VM and captures
# the disk.
echo "sn118-preview: bake complete"
trap - EXIT
