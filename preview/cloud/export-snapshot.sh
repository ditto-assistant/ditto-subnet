#!/bin/sh
set -eu

output="${1:?usage: export-snapshot.sh OUTPUT_DUMP}"
case "$output" in
  /tmp/sn118-preview-*-*.dump) ;;
  *)
    echo "refusing unsafe preview snapshot path" >&2
    exit 2
    ;;
esac
run_identity="${output#/tmp/sn118-preview-}"
run_identity="${run_identity%.dump}"
case "$run_identity" in
  *[!0-9-]* | -* | *- | *-*-*)
    echo "refusing unsafe preview snapshot identity" >&2
    exit 2
    ;;
esac

umask 077
sudo -u postgres pg_dump \
  -Fc \
  --no-owner \
  --no-privileges \
  --file="$output" \
  ditto_platform_prod
sudo chown "$(id -u):$(id -g)" "$output"
chmod 0600 "$output"
