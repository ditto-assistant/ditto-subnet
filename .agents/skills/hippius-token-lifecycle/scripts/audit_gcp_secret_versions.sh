#!/usr/bin/env bash
set -euo pipefail

project="ditto-app-dev"
max_age_days="23"

while (($#)); do
  case "$1" in
    --project)
      project="${2:?--project requires a value}"
      shift 2
      ;;
    --max-age-days)
      max_age_days="${2:?--max-age-days requires a value}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

if ! [[ "$max_age_days" =~ ^[0-9]+$ ]] || ((max_age_days < 1)); then
  echo "--max-age-days must be a positive integer" >&2
  exit 64
fi

secret_names=(
  platform-coding-catalog-access-key
  platform-coding-catalog-secret-key
  platform-coding-catalog-curator-access-key
  platform-coding-catalog-curator-secret-key
  platform-coding-hippius-evidence-access-key
  platform-coding-hippius-evidence-secret-key
)

printf 'SECRET\tLATEST_ENABLED_VERSION\tCREATED_UTC\tAGE_DAYS\tSTATUS\n'
audit_status=0
for secret_name in "${secret_names[@]}"; do
  version_json="$(
    gcloud secrets versions list "$secret_name" \
      --project="$project" \
      --filter='state=ENABLED' \
      --sort-by='~createTime' \
      --limit=1 \
      --format=json
  )"
  if [[ "$version_json" == '[]' ]]; then
    printf '%s\t-\t-\t-\tMISSING\n' "$secret_name"
    audit_status=2
    continue
  fi

  row="$(python3 - "$secret_name" "$max_age_days" "$version_json" <<'PY'
from __future__ import annotations

import datetime as dt
import json
import sys

secret_name, maximum, payload = sys.argv[1], int(sys.argv[2]), sys.argv[3]
version = json.loads(payload)[0]
created = dt.datetime.fromisoformat(version["createTime"].replace("Z", "+00:00"))
now = dt.datetime.now(dt.UTC)
age_days = max(0, int((now - created).total_seconds() // 86400))
status = "STALE" if age_days >= maximum else "OK"
name = str(version["name"]).rsplit("/", 1)[-1]
print(f"{secret_name}\t{name}\t{created.isoformat().replace('+00:00', 'Z')}\t{age_days}\t{status}")
PY
  )"
  printf '%s\n' "$row"
  if [[ "$row" == *$'\tSTALE' ]]; then
    audit_status=2
  fi
done

exit "$audit_status"
