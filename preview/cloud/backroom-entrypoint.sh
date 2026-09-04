#!/bin/sh
set -eu

case "${DITTO_ADMIN_API_TOKEN:-}" in
  *[!0-9a-f]*|'') echo "invalid preview admin token" >&2; exit 2 ;;
esac
case "${SESSION_SECRET:-}" in
  *[!0-9a-f]*|'') echo "invalid preview session secret" >&2; exit 2 ;;
esac

umask 077
env_file="$(mktemp)"
trap 'rm -f "$env_file"' EXIT
printf 'DITTO_ADMIN_API_TOKEN=%s\nSESSION_SECRET=%s\n' \
  "$DITTO_ADMIN_API_TOKEN" "$SESSION_SECRET" > "$env_file"

pnpm exec wrangler dev \
  --config wrangler.preview.jsonc \
  --env-file "$env_file" \
  --ip 0.0.0.0 \
  --port 3000
