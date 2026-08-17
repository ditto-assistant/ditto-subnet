#!/usr/bin/env bash
# Measure a read-only SELECT on production with runtime and buffer details.
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
query_script="$script_dir/query_prod_db.sh"

usage() {
    cat >&2 <<'EOF'
Usage: explain_analyze_prod_db.sh <select-or-file|->

Runs EXPLAIN (ANALYZE, BUFFERS, WAL, SETTINGS, SUMMARY) inside the same
read-only, time-bounded production transaction as query_prod_db.sh.
EOF
}

[[ $# -eq 1 ]] || { usage; exit 2; }

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/ditto-platform-explain.XXXXXX")"
query_file="$tmpdir/query.sql"
explain_file="$tmpdir/explain.sql"
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

{
    printf '%s\n' 'EXPLAIN (ANALYZE, BUFFERS, WAL, SETTINGS, SUMMARY, FORMAT TEXT)'
    cat "$query_file"
} > "$explain_file"

exec "$query_script" "$explain_file"
