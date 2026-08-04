#!/usr/bin/env bash
set -euo pipefail

# Atomically create the Worker and its encrypted bindings from Google's OAuth
# download plus Platform's existing Secret Manager value. Secret bytes are
# written only to a mode-0600 temporary file and are never printed.

oauth_json="${1:?usage: bootstrap-worker-secrets.sh GOOGLE_OAUTH_JSON}"
project="${GCP_PROJECT:-ditto-app-dev}"
platform_secret="${PLATFORM_ADMIN_SECRET_ID:-ADMIN_API_PASSWORD}"
admin_emails="${BACKROOM_ADMIN_EMAILS:?set BACKROOM_ADMIN_EMAILS to a comma-separated list of write-enabled @omniaura.ai accounts}"

: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN is required}"
: "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID is required}"

for command in gcloud jq openssl pnpm; do
  command -v "$command" >/dev/null || {
    echo "required command is unavailable: $command" >&2
    exit 1
  }
done

if [[ ! -f "$oauth_json" ]]; then
  echo "Google OAuth JSON does not exist: $oauth_json" >&2
  exit 1
fi

# Google emits either a `web` or `installed` envelope. Backroom requires a web
# client, but accepting both lets the validation below produce the useful
# redirect-URI error at runtime instead of ever displaying credential values.
if ! jq -e '
  (.web // .installed) as $client
  | ($client.client_id | type == "string" and length > 0)
  and ($client.client_secret | type == "string" and length > 0)
' "$oauth_json" >/dev/null; then
  echo "OAuth JSON is missing a client_id or client_secret" >&2
  exit 1
fi

if ! [[ ",$admin_emails," =~ @omniaura\.ai, ]]; then
  echo "BACKROOM_ADMIN_EMAILS must contain at least one @omniaura.ai address" >&2
  exit 1
fi

secret_file="$(mktemp "${TMPDIR:-/tmp}/ditto-backroom-secrets.XXXXXX")"
platform_token_file="$(mktemp "${TMPDIR:-/tmp}/ditto-platform-token.XXXXXX")"
session_secret_file="$(mktemp "${TMPDIR:-/tmp}/ditto-session-secret.XXXXXX")"
cleanup() {
  rm -f "$secret_file" "$platform_token_file" "$session_secret_file"
}
trap cleanup EXIT INT TERM
chmod 0600 "$secret_file" "$platform_token_file" "$session_secret_file"
umask 077

gcloud secrets versions access latest \
  --quiet \
  --project "$project" \
  --secret "$platform_secret" >"$platform_token_file"

if [[ ! -s "$platform_token_file" ]]; then
  echo "Platform admin secret is empty" >&2
  exit 1
fi

openssl rand -base64 48 >"$session_secret_file"
jq \
  --rawfile platform_token "$platform_token_file" \
  --rawfile session_secret "$session_secret_file" \
  --arg admin_emails "$admin_emails" \
  '{
    GOOGLE_CLIENT_ID: ((.web // .installed).client_id),
    GOOGLE_CLIENT_SECRET: ((.web // .installed).client_secret),
    SESSION_SECRET: ($session_secret | sub("\\n$"; "")),
    BACKROOM_ADMIN_EMAILS: $admin_emails,
    DITTO_ADMIN_API_TOKEN: ($platform_token | sub("\\n$"; ""))
  }' "$oauth_json" >"$secret_file"
unset admin_emails

if ! jq -e '
  all(.[]; type == "string" and length > 0)
  and (.SESSION_SECRET | length >= 32)
' "$secret_file" >/dev/null; then
  echo "Refusing to deploy incomplete Worker secrets" >&2
  exit 1
fi

# `--secrets-file` creates the Worker and encrypted bindings in one deployment.
# Future `wrangler deploy` runs preserve these values and, because wrangler.jsonc
# declares them required, fail closed if any binding is ever removed.
pnpm exec wrangler deploy --secrets-file "$secret_file"
echo "Installed five encrypted bindings on ditto-subnet-backroom."
