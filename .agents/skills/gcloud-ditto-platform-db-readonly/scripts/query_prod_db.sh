#!/usr/bin/env bash
# Run a bounded, read-only query against the production ditto-platform Postgres DB.
set -euo pipefail

readonly PROJECT="ditto-app-dev"
readonly ZONE="us-central1-a"
readonly INSTANCE="ditto-platform-prod"
readonly REMOTE_ENV="/opt/ditto-platform/.env"
readonly MAX_QUERY_BYTES=65536

usage() {
    cat >&2 <<'EOF'
Usage: query_prod_db.sh <sql-or-file|->

Examples:
  query_prod_db.sh 'SELECT count(*) FROM agents'
  query_prod_db.sh ./query.sql
  printf '%s\n' 'SELECT now()' | query_prod_db.sh -
EOF
}

[[ $# -eq 1 ]] || { usage; exit 2; }
command -v gcloud >/dev/null 2>&1 || { echo "error: gcloud is required" >&2; exit 127; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required for query validation" >&2; exit 127; }

timeout_ms="${DITTO_DB_STATEMENT_TIMEOUT_MS:-30000}"
if [[ ! "$timeout_ms" =~ ^[0-9]+$ ]] || (( timeout_ms < 1 || timeout_ms > 120000 )); then
    echo "error: DITTO_DB_STATEMENT_TIMEOUT_MS must be an integer from 1 through 120000" >&2
    exit 2
fi

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/ditto-platform-db-readonly.XXXXXX")"
query_file="$tmpdir/query.sql"
wrapper_file="$tmpdir/wrapped.sql"
cleanup() { rm -rf "$tmpdir"; }
trap cleanup EXIT

query_arg="$1"
if [[ "$query_arg" == "-" ]]; then
    cat > "$query_file"
elif [[ -f "$query_arg" ]]; then
    cp "$query_arg" "$query_file"
else
    printf '%s\n' "$query_arg" > "$query_file"
fi

query_bytes="$(wc -c < "$query_file" | tr -d ' ')"
if (( query_bytes == 0 || query_bytes > MAX_QUERY_BYTES )); then
    echo "error: query must contain 1-${MAX_QUERY_BYTES} bytes" >&2
    exit 2
fi

python3 - "$query_file" <<'PY'
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
if "\x00" in text:
    raise SystemExit("error: NUL bytes are not allowed")
if re.search(r"(?m)^\s*\\", text):
    raise SystemExit("error: psql meta-commands are not allowed")

without_comments = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
without_comments = re.sub(r"--[^\n]*", " ", without_comments)
if not re.match(r"^\s*(select|with|table|values|explain|show)\b", without_comments, re.I):
    raise SystemExit("error: query must begin with SELECT, WITH, TABLE, VALUES, EXPLAIN, or SHOW")

# PostgreSQL's read-only transaction is the authoritative barrier. This
# conservative filter also prevents transaction escape, session weakening, DDL,
# DML, and server-side file/program operations before the query reaches psql.
blocked = re.compile(
    r"\b(insert|update|delete|merge|create|alter|drop|truncate|grant|revoke|"
    r"comment|copy|call|do|vacuum|cluster|reindex|refresh|set|reset|"
    r"begin|start|commit|rollback|end|prepare|execute|deallocate|discard|lock|"
    r"listen|unlisten|notify|security\s+label)\b",
    re.I,
)
match = blocked.search(without_comments)
if match:
    raise SystemExit(f"error: blocked SQL keyword: {match.group(0)}")
PY

{
    printf '%s\n' '\set ON_ERROR_STOP on'
    printf '%s\n' '\pset pager off'
    printf '%s\n' 'BEGIN READ ONLY;'
    cat "$query_file"
    printf '\n;\n%s\n' 'ROLLBACK;'
} > "$wrapper_file"

remote_command="sudo -n -u deploy bash -lc 'set -euo pipefail; set -a; source ${REMOTE_ENV}; set +a; export PGPASSWORD=\"\$POSTGRES_PASSWORD\"; export PGOPTIONS=\"-c default_transaction_read_only=on -c statement_timeout=${timeout_ms} -c lock_timeout=5000\"; exec psql --no-password --host=\"\$POSTGRES_HOST\" --port=\"\$POSTGRES_PORT\" --username=\"\$POSTGRES_USER\" --dbname=\"\$POSTGRES_DB\" --file=-'"

gcloud compute ssh "$INSTANCE" \
    --project="$PROJECT" \
    --zone="$ZONE" \
    --tunnel-through-iap \
    --quiet \
    --command="$remote_command" < "$wrapper_file"
