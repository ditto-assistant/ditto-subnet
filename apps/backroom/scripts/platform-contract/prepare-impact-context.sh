#!/usr/bin/env bash
set -euo pipefail

diff_file="${1:-.contract-sync/platform-contract.diff}"
output_file="${2:-.contract-sync/impact-context.md}"

if [[ ! -s "$diff_file" ]]; then
  echo "contract diff is missing or empty: $diff_file" >&2
  exit 1
fi

tmp_file="$(mktemp .contract-sync/impact-context.XXXXXX)"
trap 'rm -f "$tmp_file"' EXIT

extract_changed() {
  local pattern="$1"
  sed -nE "/^[+-][^+-]/s/^[+-][[:space:]]*${pattern}.*/\\1/p" "$diff_file" |
    sort -u
}

routes="$(extract_changed '\"(\/api\/[^\"]+)\"' || true)"
schemas="$(extract_changed '([A-Z][A-Za-z0-9_]+):[[:space:]]*\{' || true)"
operations="$(extract_changed '.*operations\[\"([^\"]+)\"\]' || true)"
fields="$(extract_changed '([a-z][a-z0-9_]+)\??:[[:space:]]' |
  rg -v '^(get|put|post|delete|options|head|patch|trace|parameters|query|header|path|cookie|requestBody|responses|content|description|summary|operationId|tags)$' || true)"

{
  echo '# Contract impact context'
  echo
  echo 'Generated deterministically before the agent runs. Start here; do not rediscover repository structure or inspect git history.'
  echo
  echo '## Generated diff size'
  echo
  git diff --cached --stat
  echo
  echo '## Changed API routes'
  echo
  if [[ -n "$routes" ]]; then printf '%s\n' "$routes" | sed 's/^/- `/; s/$/`/'; else echo '- None'; fi
  echo
  echo '## Changed OpenAPI schemas'
  echo
  if [[ -n "$schemas" ]]; then printf '%s\n' "$schemas" | sed 's/^/- `/; s/$/`/'; else echo '- None'; fi
  echo
  echo '## Changed operation IDs'
  echo
  if [[ -n "$operations" ]]; then printf '%s\n' "$operations" | sed 's/^/- `/; s/$/`/'; else echo '- None'; fi
  echo
  echo '## Changed wire fields'
  echo
  if [[ -n "$fields" ]]; then printf '%s\n' "$fields" | sed 's/^/- `/; s/$/`/'; else echo '- None'; fi
  echo
  echo '## Existing Backroom consumers'
  echo
  echo 'Files below already reference at least one changed field or operation term. Read these first; absence is evidence that a contract surface may be Platform-internal.'
  echo
  if [[ -n "$fields$operations" ]]; then
    patterns=()
    while IFS= read -r term; do
      [[ -n "$term" ]] && patterns+=( -e "$term" )
    done < <(printf '%s\n%s\n' "$fields" "$operations" | sort -u)
    rg -l -F "${patterns[@]}" src \
      --glob '!src/generated/**' \
      --glob '!src/routeTree.gen.ts' 2>/dev/null |
      sort -u |
      head -80 |
      sed 's/^/- `/' |
      sed 's/$/`/' || true
  else
    echo '- None found'
  fi
  echo
  echo '## Execution boundary'
  echo
  echo '- The workflow runs full typecheck, tests, build, and diff validation after implementation.'
  echo '- During implementation, run only focused tests needed to develop a changed behavior.'
  echo '- Do not run the full suite or build; repeating workflow validation wastes time and tokens.'
} > "$tmp_file"

mv "$tmp_file" "$output_file"
trap - EXIT
