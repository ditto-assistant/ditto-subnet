#!/usr/bin/env bash
# Fixed-target, read-only PostgreSQL recovery diagnostics. No SQL, credentials,
# restart, promotion, WAL modification, or arbitrary remote command surface.
set -euo pipefail
[[ $# == 0 ]] || { echo 'usage: inspect_platform_postgres.sh' >&2; exit 2; }
readonly remote_command=$(cat <<'REMOTE'
set -eu
date -u
printf '\nFilesystem capacity\n'
df -hT / /var/lib/postgresql /opt/ditto 2>/dev/null || true
df -i / /var/lib/postgresql /opt/ditto 2>/dev/null || true
printf '\nPostgreSQL clusters\n'
sudo -n pg_lsclusters
printf '\nReadiness\n'
pg_isready -h 127.0.0.1 -p 5432 -t 5 || true
printf '\nPostgreSQL service state\n'
systemctl is-active postgresql || true
printf '\nBounded recovery log diagnostics\n'
sudo -n python3 - <<'PY'
from pathlib import Path
import re
pattern = re.compile(r'\b(?:LOG|FATAL|PANIC):\s+(?:the database system|database system|redo |checkpoint|recovery|invalid checkpoint|could not (?:write|fsync|open|locate)|server process|startup process|terminating any other active server processes|all server processes terminated|remaining connection slots)')
for root in [Path('/opt/ditto/logs/postgresql'), Path('/var/log/postgresql')]:
    if not root.is_dir():
        continue
    paths = sorted((p for p in root.iterdir() if p.is_file() and not p.is_symlink() and p.suffix == '.log'), key=lambda p:p.stat().st_mtime, reverse=True)[:2]
    for path in paths:
        print(str(path))
        with path.open('rb') as stream:
            stream.seek(max(0,path.stat().st_size-4*1024*1024))
            rows=stream.read().decode(errors='replace').splitlines()
        for row in [r for r in rows if pattern.search(r) and 'STATEMENT:' not in r][-60:]:
            print(row[:1200])
PY
REMOTE
)
exec gcloud compute ssh ditto-pg-platform --project=ditto-app-dev --zone=us-central1-a --tunnel-through-iap --quiet --command="$remote_command"
