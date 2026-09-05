#!/usr/bin/env bash
set -euo pipefail

source_dump="${1:?usage: sanitize-snapshot.sh SOURCE_DUMP OUTPUT_DUMP}"
output_dump="${2:?usage: sanitize-snapshot.sh SOURCE_DUMP OUTPUT_DUMP}"
test -f "$source_dump"
test ! -e "$output_dump"

script_dir="$(cd "$(dirname "$0")" && pwd)"
container="sn118-preview-sanitize-$RANDOM-$$"
cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --name "$container" \
  -e POSTGRES_USER=ditto -e POSTGRES_PASSWORD=preview-sanitizer \
  -e POSTGRES_DB=preview_sanitize \
  -v "$script_dir/sanitize.sql:/sanitize.sql:ro" \
  pgvector/pgvector:pg17 >/dev/null

ready=false
for _ in {1..60}; do
  # The official image starts a temporary Postgres while initializing and then
  # replaces PID 1 with the durable server. pg_isready can succeed during that
  # temporary window, immediately before the server shuts down.
  if docker exec "$container" sh -c 'test "$(cat /proc/1/comm)" = postgres' \
    && test "$(docker exec "$container" psql -v ON_ERROR_STOP=1 -Atq -U ditto -d preview_sanitize -c 'SELECT 1' 2>/dev/null)" = 1; then
    ready=true
    break
  fi
  sleep 1
done
[ "$ready" = true ] || {
  docker logs "$container" >&2
  echo "sanitizer Postgres did not become ready" >&2
  exit 1
}

docker cp "$source_dump" "$container:/tmp/source.dump" >/dev/null
if ! docker exec "$container" sh -c 'exec "$@" >/tmp/restore.log 2>&1' sh \
  pg_restore --schema-only --no-owner --no-privileges \
  -U ditto -d preview_sanitize /tmp/source.dump; then
  echo "snapshot schema restore failed; detailed output withheld from CI" >&2
  exit 1
fi
if ! docker exec "$container" sh -c 'exec "$@" >/tmp/alembic-restore.log 2>&1' sh \
  pg_restore --data-only --table=alembic_version --no-owner --no-privileges \
  -U ditto -d preview_sanitize /tmp/source.dump; then
  echo "snapshot migration marker restore failed; detailed output withheld from CI" >&2
  exit 1
fi

# pgcrypto supplies digest() for deterministic pseudonyms.
docker exec "$container" psql -v ON_ERROR_STOP=1 -U ditto -d preview_sanitize \
  -c 'CREATE EXTENSION IF NOT EXISTS pgcrypto' >/dev/null
docker exec "$container" psql -v ON_ERROR_STOP=1 -U ditto -d preview_sanitize -f /sanitize.sql >/dev/null

docker exec "$container" pg_dump -Fc --no-owner --no-privileges -U ditto -d preview_sanitize -f /tmp/sanitized.dump
docker cp "$container:/tmp/sanitized.dump" "$output_dump" >/dev/null
chmod 0600 "$output_dump"
sha256sum "$output_dump"
