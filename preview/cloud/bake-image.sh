#!/usr/bin/env bash
# Trusted-controller entrypoint. This file runs from the default branch only.
#
# Bakes the sn118-preview-base image family: boot one throwaway VM on stock
# Ubuntu, let preview/cloud/startup.sh install the toolchain and hand off to
# preview/cloud/bake.sh, then capture its boot disk as a custom image.
#
# The bake VM deliberately runs the *same* startup script a preview does. That
# is the whole reason the resulting image cannot drift from what a preview boot
# expects: there is no second install path to keep in sync, and startup.sh's own
# toolchain guard is what makes the baked image skip apt on the next boot.
#
# It also runs with no service account and no scopes at all. Nothing on it needs
# cloud API access -- the image is captured from outside, by this script's
# identity -- so the bake identity needs no actAs grant, and the credential-empty
# property of the preview subnet is preserved rather than excepted.
set -euo pipefail
cd "$(dirname "$0")/../.."
. preview/cloud/lib.sh

preview_require PREVIEW_BAKE_SHA GITHUB_REPOSITORY \
  GCP_PREVIEW_NETWORK GCP_PREVIEW_SUBNETWORK GCP_PREVIEW_ZONE
[[ "$PREVIEW_BAKE_SHA" =~ ^[0-9a-f]{40}$ ]] || preview_die "PREVIEW_BAKE_SHA must be an exact commit"
test "$GITHUB_REPOSITORY" = "ditto-assistant/ditto-subnet" || preview_die "wrong repository"

# The sn118- prefix is not cosmetic: the prune step at the end deletes every
# image in this family beyond the newest few, so the family name must never be
# able to name someone else's.
image_family="${GCP_PREVIEW_IMAGE_FAMILY:-sn118-preview-base}"
[[ "$image_family" =~ ^sn118-[a-z0-9-]{1,50}[a-z0-9]$ ]] || preview_die "image family must be sn118-*"

# Small on purpose. A custom image reports diskSizeGb equal to the disk it was
# captured from, and --boot-disk-size below that value is a hard error in
# instances create -- so a big bake disk would silently raise the floor on
# GCP_PREVIEW_DISK_SIZE for every preview. 32GB fits the toolchain, the four
# pulled images, the five built ones and the BuildKit cache with room to spare.
bake_disk_size="${PREVIEW_BAKE_DISK_SIZE:-32GB}"
[[ "$bake_disk_size" =~ ^[0-9]+(GB|TB)?$ ]] || preview_die "invalid bake disk size"
# Same shape as a preview VM, so the layers being warmed are built by the same
# amount of parallelism they will be rebuilt with.
bake_machine_type="${PREVIEW_BAKE_MACHINE_TYPE:-e2-standard-8}"
[[ "$bake_machine_type" =~ ^[a-z0-9-]+$ ]] || preview_die "invalid bake machine type"
keep_images="${PREVIEW_BAKE_KEEP_IMAGES:-3}"
[[ "$keep_images" =~ ^[1-9][0-9]*$ ]] || preview_die "invalid image retention count"

instance="sn118-preview-bake"
image_name="${image_family}-$(date -u +%Y%m%d-%H%M)-${PREVIEW_BAKE_SHA:0:7}"

completed=false
cleanup() {
  # The VM is worthless the moment its disk is captured, and it is an
  # 8-vCPU machine, so deleting it is not optional bookkeeping. This runs on
  # success, on failure, and on a cancelled workflow.
  gcloud compute instances delete "$instance" --zone "$GCP_PREVIEW_ZONE" --quiet >/dev/null 2>&1 || true
  if [ "$completed" != true ]; then
    echo "bake: failed; no new image was added to family ${image_family}" >&2
  fi
}
trap cleanup EXIT

# A previous run that died between create and delete would otherwise collide on
# the fixed name. Concurrency in the workflow keeps this to one run at a time.
gcloud compute instances delete "$instance" --zone "$GCP_PREVIEW_ZONE" --quiet >/dev/null 2>&1 || true

metadata_file="$(mktemp)"
printf '%s' "$PREVIEW_BAKE_SHA" >"$metadata_file"

gcloud compute instances create "$instance" \
  --zone "$GCP_PREVIEW_ZONE" \
  --machine-type "$bake_machine_type" \
  --boot-disk-size "$bake_disk_size" \
  --boot-disk-type pd-balanced \
  --image-family ubuntu-2404-lts-amd64 \
  --image-project ubuntu-os-cloud \
  --network "$GCP_PREVIEW_NETWORK" \
  --subnet "$GCP_PREVIEW_SUBNETWORK" \
  --no-service-account \
  --no-scopes \
  --tags sn118-preview \
  --labels "system=sn118-preview,preview-role=bake" \
  --metadata-from-file "startup-script=preview/cloud/startup.sh,preview-bake-sha=${metadata_file}" \
  --shielded-secure-boot \
  --shielded-vtpm \
  --shielded-integrity-monitoring \
  --quiet >&2
rm -f "$metadata_file"

# The serial console is the only channel: the bake VM has no credentials and no
# inbound port. bake.sh prints "bake complete" as its last act, and both
# startup.sh and bake.sh print the same "startup failed" sentinel provision.sh
# already watches for, so a broken build is reported in about a minute instead of
# burning the whole timeout.
baked=false
for attempt in $(seq 1 360); do
  console="$(gcloud compute instances get-serial-port-output "$instance" \
    --zone "$GCP_PREVIEW_ZONE" --format='value(contents)' 2>/dev/null || true)"
  if grep -q 'sn118-preview: bake complete' <<<"$console"; then
    baked=true
    break
  fi
  if grep -q 'sn118-preview: startup failed' <<<"$console"; then
    tail -n 120 <<<"$console" >&2
    preview_die "bake script failed on the VM; console tail above"
  fi
  sleep 10
done

if [ "$baked" != true ]; then
  gcloud compute instances get-serial-port-output "$instance" --zone "$GCP_PREVIEW_ZONE" \
    --format='value(contents)' 2>/dev/null | tail -n 120 >&2 || true
  preview_die "bake did not complete within 60 minutes; console tail above"
fi

# Stop before capturing so the filesystem is quiesced and the Docker state on
# disk is consistent. Imaging a running disk is allowed with --force but risks
# capturing a half-written layer, which would be invisible until a preview built
# on top of it.
gcloud compute instances stop "$instance" --zone "$GCP_PREVIEW_ZONE" --quiet >&2

# Guest OS features are not inherited from the source disk's image, and
# provision.sh creates every preview with --shielded-secure-boot, which requires
# UEFI_COMPATIBLE. Dropping these silently produces an image that cannot boot a
# preview.
gcloud compute images create "$image_name" \
  --source-disk "$instance" \
  --source-disk-zone "$GCP_PREVIEW_ZONE" \
  --family "$image_family" \
  --guest-os-features UEFI_COMPATIBLE,VIRTIO_SCSI_MULTIQUEUE,GVNIC \
  --labels "system=sn118-preview,preview-role=base,preview-bake-sha=${PREVIEW_BAKE_SHA:0:12}" \
  --description "sn118 preview base: docker toolchain and warm compose build cache at ${PREVIEW_BAKE_SHA}" \
  --quiet >&2

# Keep a few older images so a bad bake can be rolled back by pointing the family
# at a previous one, but do not accumulate them: each is billed for its full
# nominal size whatever the guest actually used.
# --no-standard-images because the default listing includes the public images
# from ubuntu-os-cloud and friends, and this loop deletes what it is handed.
mapfile -t stale < <(gcloud compute images list \
  --no-standard-images \
  --filter="family=${image_family}" \
  --sort-by=~creationTimestamp \
  --format='value(name)' | tail -n "+$(( keep_images + 1 ))")
for old in "${stale[@]:-}"; do
  [ -n "$old" ] || continue
  gcloud compute images delete "$old" --quiet >&2 || true
done

completed=true
jq -n --arg image "$image_name" --arg family "$image_family" --arg sha "$PREVIEW_BAKE_SHA" \
  '{image:$image,family:$family,sha:$sha}'
