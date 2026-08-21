#!/usr/bin/env bash
# Restore a Platform Postgres dump into the preview database, then align the
# overlay metagraph. Does not target finney. Usage:
#   PREVIEW_DATABASE_URL=postgres://ditto:preview@127.0.0.1:5433/ditto_platform_preview \
#     ./scripts/preview-restore-snapshot.sh /path/to/dump.dump
set -euo pipefail
dump="${1:?usage: preview-restore-snapshot.sh DUMP_FILE}"
url="${PREVIEW_DATABASE_URL:?set PREVIEW_DATABASE_URL to the preview Postgres}"
case "$url" in
  *finney*|*heyditto.ai*|*ditto_platform_prod*)
    echo "refusing to restore into what looks like production" >&2
    exit 2
    ;;
esac
pg_restore --clean --if-exists --no-owner --dbname="$url" "$dump"
uv run python -m ditto.preview ctl align_from_db --database-url "$url"
