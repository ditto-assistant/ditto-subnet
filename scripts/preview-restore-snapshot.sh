#!/usr/bin/env bash
# Restore a Platform Postgres dump into the preview database, then align the
# overlay metagraph. Does not target finney. Usage:
#   PREVIEW_DATABASE_URL=postgres://ditto:preview@127.0.0.1:5433/ditto_platform_preview \
#     ./scripts/preview-restore-snapshot.sh /path/to/dump.dump
set -euo pipefail
dump="${1:?usage: preview-restore-snapshot.sh DUMP_FILE}"
url="${PREVIEW_DATABASE_URL:?set PREVIEW_DATABASE_URL to the preview Postgres}"
python3 - "$url" <<'PY'
import sys
from urllib.parse import unquote, urlparse

parsed = urlparse(sys.argv[1])
allowed_hosts = {"127.0.0.1", "localhost", "::1"}
database = unquote(parsed.path.lstrip("/"))
user = unquote(parsed.username or "")
password = unquote(parsed.password or "")
if (
    parsed.scheme not in {"postgres", "postgresql"}
    or parsed.hostname not in allowed_hosts
    or parsed.port != 5433
    or database != "ditto_platform_preview"
    or user != "ditto"
    or password != "preview"
):
    raise SystemExit(
        "refusing destructive restore: target must be "
        "ditto@127.0.0.1:5433/ditto_platform_preview"
    )
PY
pg_restore --clean --if-exists --no-owner --dbname="$url" "$dump"
uv run python -m ditto.preview ctl align_from_db --database-url "$url"
