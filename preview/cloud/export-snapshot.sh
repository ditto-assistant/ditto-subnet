#!/bin/sh
set -eu

# Stage on disk, never /tmp: Debian 13 mounts /tmp as a RAM-backed tmpfs (half
# of the database VM's 8 GB), which the archive outgrew in 2026-09.
staging_dir="${SN118_PREVIEW_STAGING_DIR:-/var/tmp}"
legacy_dir="${SN118_PREVIEW_LEGACY_DIR:-/tmp}"
output="${1:?usage: export-snapshot.sh OUTPUT_DUMP}"
case "$output" in
  "$staging_dir"/sn118-preview-*-*.dump) ;;
  *)
    echo "refusing unsafe preview snapshot path" >&2
    exit 2
    ;;
esac
run_identity="${output#"$staging_dir"/sn118-preview-}"
run_identity="${run_identity%.dump}"
case "$run_identity" in
  *[!0-9-]* | -* | *- | *-*-*)
    echo "refusing unsafe preview snapshot identity" >&2
    exit 2
    ;;
esac

umask 077

# A failed pg_dump leaves its partial archive owned by postgres in a sticky
# directory, where the workflow's unprivileged cleanup could not remove it.
# Those leftovers filled /tmp and failed every run from 2026-10-01. Reclaim any
# earlier run's archive first (the workflow's concurrency group keeps runs
# serial) and always remove this run's archive unless it completed.
for dir in "$legacy_dir" "$staging_dir"; do
  sudo find "$dir" -maxdepth 1 -type f -name 'sn118-preview-*.dump' \
    ! -path "$output" -print -delete >&2
done
df -Pk "$legacy_dir" "$staging_dir" >&2

completed=0
remove_incomplete() {
  if [ "$completed" -ne 1 ]; then
    sudo rm -f "$output"
  fi
}
trap remove_incomplete EXIT

# The staging disk also holds PGDATA and WAL. The uncompressed database size
# bounds the archive; refuse an export that could push the disk toward the
# 2026-09-08 ENOSPC crash loop.
database_bytes="$(
  sudo -u postgres psql -XAtq \
    -c "SELECT pg_database_size('ditto_platform_prod')"
)"
case "$database_bytes" in
  '' | *[!0-9]*)
    echo "could not read the database size" >&2
    exit 1
    ;;
esac
total_kib="$(df -Pk "$staging_dir" | awk 'NR == 2 { print $2 }')"
available_kib="$(df -Pk "$staging_dir" | awk 'NR == 2 { print $4 }')"
case "${total_kib}:${available_kib}" in
  :* | *: | *[!0-9:]*)
    echo "could not read free space on ${staging_dir}" >&2
    exit 1
    ;;
esac
total_bytes=$((total_kib * 1024))
available_bytes=$((available_kib * 1024))
reserve_bytes=$((total_bytes / 5))
min_reserve_bytes=$((10 * 1024 * 1024 * 1024))
if [ "$reserve_bytes" -lt "$min_reserve_bytes" ]; then
  reserve_bytes="$min_reserve_bytes"
fi
if [ "$available_bytes" -lt $((database_bytes + reserve_bytes)) ]; then
  echo "refusing export: ${available_bytes} bytes free on ${staging_dir}," \
    "need ${database_bytes} for the archive plus a ${reserve_bytes} reserve" >&2
  exit 1
fi

sudo -u postgres pg_dump \
  -Fc \
  --no-owner \
  --no-privileges \
  --file="$output" \
  ditto_platform_prod
sudo chown "$(id -u):$(id -g)" "$output"
chmod 0600 "$output"
completed=1
