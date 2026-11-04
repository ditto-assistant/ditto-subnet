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
trap 'rm -f "$env_file" "${key:-/nonexistent}.tmp" "${cert:-/nonexistent}.tmp"' EXIT
printf 'DITTO_ADMIN_API_TOKEN=%s\nSESSION_SECRET=%s\n' \
  "$DITTO_ADMIN_API_TOKEN" "$SESSION_SECRET" > "$env_file"
if [ "${PREVIEW_PR:-0}" = 3359 ] && [ "${PREVIEW_PROFILE:-}" = stack ]; then
  printf '%s\n' 'BACKROOM_ADMIN_EMAILS=synthetic-preview-only@omniaura.ai' >> "$env_file"
fi

# The public gateway terminates TLS, but Wrangler must also receive HTTPS so
# the app's origin and security-header checks see the browser's scheme. Keep
# this preview-only key private; the gateway mounts only its certificate.
# The seven-day certificate outlives the provisioner's maximum 48-hour lease.
key=/run/preview-backroom-key/key.pem
cert=/run/preview-backroom-cert/cert.pem
if [ ! -s "$key" ] && [ ! -s "$cert" ]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 7 \
    -subj '/CN=backroom' -addext 'subjectAltName=DNS:backroom' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'extendedKeyUsage=serverAuth' \
    -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
    -keyout "$key.tmp" -out "$cert.tmp" >/dev/null
  chmod 0600 "$key.tmp"
  chmod 0644 "$cert.tmp"
  mv "$key.tmp" "$key"
  mv "$cert.tmp" "$cert"
elif [ ! -s "$key" ] || [ ! -s "$cert" ]; then
  echo 'incomplete preview TLS identity; recreate this disposable preview to recover' >&2
  exit 2
fi

pnpm exec wrangler dev \
  --config wrangler.preview.jsonc \
  --env-file "$env_file" \
  --ip 0.0.0.0 \
  --port 3000 \
  --local-protocol https \
  --https-key-path "$key" \
  --https-cert-path "$cert"
