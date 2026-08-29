#!/usr/bin/env bash
# gen-schema.sh — render services/model-relay/db/schema.sql from the REAL
# Alembic migration chain in apps/platform.
#
# Alembic owns the schema. The Go relay never migrates anything; sqlc merely
# needs DDL to compile queries against, and that DDL must be GENERATED from
# the finalized migration chain, never hand-transcribed. This mirrors how
# ditto-assistant/backend renders schema.sql from its migrations before
# running sqlc (scripts/pg-schema-gen.sh there).
#
# Flow:
#   1. ensure the monorepo test Postgres (ditto-platform-test-postgres,
#      host port 15433, postgres:16-alpine, ditto_test/ditto_test) is up —
#      started here with the same parameters the apps/platform pgharness
#      uses if it is absent;
#   2. create a scratch database on it;
#   3. run the real Alembic chain against the scratch database
#      (cd apps/platform && uv run alembic upgrade head with POSTGRES_* env);
#   4. pg_dump --schema-only --no-owner --no-privileges into
#      services/model-relay/db/schema.sql (pg_dump is run INSIDE the
#      container so client and server versions always match);
#   5. drop the scratch database.
#
# Usage: services/model-relay/scripts/gen-schema.sh
# Requires: docker, uv (for apps/platform), a checkout of the monorepo.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${SERVICE_DIR}/../.." && pwd)"
PLATFORM_DIR="${REPO_ROOT}/apps/platform"
OUT_FILE="${SERVICE_DIR}/db/schema.sql"

CONTAINER_NAME="${DITTO_TEST_POSTGRES_CONTAINER:-ditto-platform-test-postgres}"
CONTAINER_IMAGE="${DITTO_TEST_POSTGRES_IMAGE:-postgres:16-alpine}"
HOST_PORT="${DITTO_TEST_POSTGRES_PORT:-15433}"
ADMIN_USER="ditto_test"
ADMIN_PASSWORD="ditto_test"
ADMIN_DB="postgres"

SCRATCH_DB="model_relay_schema_$$"

log() { echo "[gen-schema] $*" >&2; }

psql_admin() {
  docker exec -i "${CONTAINER_NAME}" psql -v ON_ERROR_STOP=1 \
    -U "${ADMIN_USER}" -d "${ADMIN_DB}" -qAt -c "$1"
}

# ── 1. Ensure the ambient test Postgres is running ──────────────────────────
running="$(docker inspect -f '{{.State.Running}}' "${CONTAINER_NAME}" 2>/dev/null || true)"
if [[ "${running}" != "true" ]]; then
  log "container ${CONTAINER_NAME} not running; starting it (same parameters as apps/platform pgharness)"
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  docker run -d --name "${CONTAINER_NAME}" \
    -e POSTGRES_USER="${ADMIN_USER}" \
    -e POSTGRES_PASSWORD="${ADMIN_PASSWORD}" \
    -e POSTGRES_DB="${ADMIN_DB}" \
    -p "127.0.0.1:${HOST_PORT}:5432" \
    "${CONTAINER_IMAGE}" >/dev/null
fi
# A cold postgres:16-alpine on a busy CI runner can take well over 30s to
# finish initdb; 30 one-second probes flaked there. pg_isready is the cheap
# in-container probe; the psql check stays as the authoritative gate.
# Do not call psql_admin from `&&` under `set -e`: a function's failing
# docker exec can abort the script before the 120s loop finishes (release
# then reports "after 120s" on a ~few-second cold start).
postgres_ready=0
for i in $(seq 1 120); do
  if docker exec "${CONTAINER_NAME}" pg_isready -U "${ADMIN_USER}" -d "${ADMIN_DB}" >/dev/null 2>&1; then
    if docker exec -i "${CONTAINER_NAME}" psql -v ON_ERROR_STOP=1 \
      -U "${ADMIN_USER}" -d "${ADMIN_DB}" -qAt -c "SELECT 1" >/dev/null 2>&1; then
      postgres_ready=1
      break
    fi
  fi
  [[ $((i % 15)) -eq 0 ]] && log "still waiting for postgres (${i}s)"
  sleep 1
done
if [[ "${postgres_ready}" != 1 ]]; then
  log "postgres did not become ready after 120s"
  exit 1
fi

# ── 2. Scratch database ─────────────────────────────────────────────────────
cleanup() {
  psql_admin "DROP DATABASE IF EXISTS ${SCRATCH_DB}" >/dev/null 2>&1 || true
}
trap cleanup EXIT
log "creating scratch database ${SCRATCH_DB}"
# The official Postgres image briefly accepts connections from its temporary
# initdb postmaster before stopping it and starting the final postmaster.  A
# cold CI runner can therefore pass the readiness probe above and lose the
# server between that probe and CREATE DATABASE.  Provision idempotently and
# retry across that handoff instead of treating the transient socket failure
# as schema drift.
scratch_ready=0
for i in $(seq 1 120); do
  if psql_admin \
    "SELECT 1 FROM pg_database WHERE datname = '${SCRATCH_DB}'" \
    2>/dev/null | grep -qx 1; then
    scratch_ready=1
    break
  fi
  if psql_admin "CREATE DATABASE ${SCRATCH_DB}" >/dev/null 2>&1; then
    scratch_ready=1
    break
  fi
  [[ $((i % 15)) -eq 0 ]] && log "still waiting to create scratch database (${i}s)"
  sleep 1
done
if [[ "${scratch_ready}" != 1 ]]; then
  log "could not create scratch database after 120s"
  exit 1
fi

# ── 3. Real Alembic chain ───────────────────────────────────────────────────
log "running alembic upgrade head against ${SCRATCH_DB}"
(
  cd "${PLATFORM_DIR}"
  POSTGRES_HOST=localhost \
  POSTGRES_PORT="${HOST_PORT}" \
  POSTGRES_USER="${ADMIN_USER}" \
  POSTGRES_PASSWORD="${ADMIN_PASSWORD}" \
  POSTGRES_DB="${SCRATCH_DB}" \
  uv run alembic upgrade head
)

# ── 4. Dump schema ──────────────────────────────────────────────────────────
log "dumping schema into ${OUT_FILE}"
mkdir -p "$(dirname "${OUT_FILE}")"
{
  cat <<'HEADER'
-- Code generated by services/model-relay/scripts/gen-schema.sh. DO NOT EDIT.
--
-- This file is a READ-ONLY MIRROR of the apps/platform Alembic schema, used
-- only so sqlc can compile the relay's queries. It is NOT a migration and is
-- never applied to production. Alembic (apps/platform/alembic) owns the
-- schema; the Go relay only reads/writes existing tables.
--
-- Regenerate with:
--   services/model-relay/scripts/gen-schema.sh
-- (runs the real Alembic chain against a scratch database on the monorepo
-- test Postgres ditto-platform-test-postgres:15433, then pg_dump
-- --schema-only --no-owner --no-privileges.)
HEADER
  # Strip psql meta-commands (\restrict / \unrestrict, emitted by patched
  # pg_dump 16.x): they are psql directives, not SQL, and sqlc's parser
  # rejects them. Also strip the "Dumped from/by ... version" comments: they
  # vary with the point release of the Postgres container (16.14 vs 16.15)
  # and would make the CI drift check fail on environment, not schema.
  # Both are purely mechanical line filters.
  docker exec "${CONTAINER_NAME}" pg_dump \
    --schema-only --no-owner --no-privileges \
    -U "${ADMIN_USER}" "${SCRATCH_DB}" \
    | grep -v '^\\' | grep -v '^-- Dumped \(from database\|by pg_dump\) version'
} > "${OUT_FILE}"

# pg_dump terminates with multiple blank lines. Keep one final newline but no
# trailing blank lines so the generated artifact is stable under git diff
# --check as well as the schema drift guard.
python3 - "${OUT_FILE}" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
path.write_text(path.read_text().rstrip() + "\n")
PY

log "done: $(wc -l < "${OUT_FILE}") lines"
