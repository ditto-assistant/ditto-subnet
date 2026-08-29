#!/usr/bin/env bash
set -euo pipefail

source_dump="${1:?usage: sanitize-snapshot.sh SOURCE_DUMP OUTPUT_DUMP}"
output_dump="${2:?usage: sanitize-snapshot.sh SOURCE_DUMP OUTPUT_DUMP}"
test -f "$source_dump"
test ! -e "$output_dump"

script_dir="$(cd "$(dirname "$0")" && pwd)"
temp_dir="$(mktemp -d)"
container="sn118-preview-sanitize-$RANDOM-$$"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf "$temp_dir"
}
trap cleanup EXIT

cp "$source_dump" "$temp_dir/source.dump"
chmod 0600 "$temp_dir/source.dump"
docker run -d --name "$container" \
  -e POSTGRES_USER=ditto -e POSTGRES_PASSWORD=preview-sanitizer \
  -e POSTGRES_DB=preview_sanitize \
  -v "$temp_dir:/work" -v "$script_dir/sanitize.sql:/sanitize.sql:ro" \
  postgres:16-alpine >/dev/null

for _ in {1..60}; do
  docker exec "$container" pg_isready -U ditto -d preview_sanitize >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$container" pg_isready -U ditto -d preview_sanitize >/dev/null
docker exec "$container" pg_restore --no-owner --no-privileges -U ditto -d preview_sanitize /work/source.dump

# pgcrypto supplies digest() for deterministic pseudonyms.
docker exec "$container" psql -v ON_ERROR_STOP=1 -U ditto -d preview_sanitize \
  -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto' >/dev/null
docker exec "$container" psql -v ON_ERROR_STOP=1 -U ditto -d preview_sanitize -f /sanitize.sql >/dev/null

docker exec "$container" pg_dump -Fc --no-owner --no-privileges -U ditto -d preview_sanitize -f /work/sanitized.dump
cp "$temp_dir/sanitized.dump" "$output_dump"
chmod 0600 "$output_dump"
sha256sum "$output_dump"
