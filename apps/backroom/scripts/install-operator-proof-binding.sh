#!/usr/bin/env bash
set -euo pipefail

# Install the already approved shared proof value as an encrypted Backroom
# binding. Wrangler creates and deploys a Worker version immediately.
if [[ "${1:-}" != "INSTALL BACKROOM OPERATOR PROOF BINDING" ]]; then
  echo 'usage: install-operator-proof-binding.sh "INSTALL BACKROOM OPERATOR PROOF BINDING"' >&2
  exit 2
fi

: "${CLOUDFLARE_API_TOKEN:?CLOUDFLARE_API_TOKEN is required}"
: "${CLOUDFLARE_ACCOUNT_ID:?CLOUDFLARE_ACCOUNT_ID is required}"
: "${BACKROOM_OAUTH_KV_ID:?BACKROOM_OAUTH_KV_ID is required}"
project="${GCP_PROJECT:-ditto-app-dev}"
secret_id="${BACKROOM_OPERATOR_PROOF_SECRET_ID:-backroom-platform-operator-proof}"
for command in gcloud pnpm git node wc; do
  command -v "$command" >/dev/null || {
    echo "required command is unavailable: $command" >&2
    exit 1
  }
done

repo_root="$(git -C "$(dirname "$0")/../../.." rev-parse --show-toplevel)"
if [[ -n "$(git -C "$repo_root" status --porcelain)" ]]; then
  echo "refusing to deploy a Worker from a dirty checkout" >&2
  exit 1
fi
release_tag="$(git -C "$repo_root" describe --exact-match --tags HEAD 2>/dev/null || true)"
if [[ ! "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "refusing to deploy a Worker outside an exact semantic release tag" >&2
  exit 1
fi

secret_file="$(mktemp "${TMPDIR:-/tmp}/ditto-operator-proof.XXXXXX")"
config_file=""
trap 'rm -f "$secret_file" "$config_file"' EXIT INT TERM
chmod 0600 "$secret_file"
gcloud secrets versions access latest \
  --quiet --project "$project" --secret "$secret_id" >"$secret_file"
if (( $(wc -c <"$secret_file") != 64 )) || ! LC_ALL=C grep -Eq '^[0-9a-f]{64}$' "$secret_file"; then
  echo "operator proof secret must be exactly 64 lowercase hex bytes without a newline" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
config_file="$(node - "$PWD/wrangler.jsonc" <<'NODE'
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const source = process.argv[2]
const id = process.env.BACKROOM_OAUTH_KV_ID
if (!/^[0-9a-f]{32}$/.test(id)) throw new Error('BACKROOM_OAUTH_KV_ID must be 32 lowercase hex bytes')
const original = fs.readFileSync(source, 'utf8')
const placeholder = 'OAUTH_KV_NAMESPACE_ID_INJECTED_AT_DEPLOY'
if (!original.includes(placeholder)) throw new Error('Wrangler config has no OAuth KV placeholder')
const target = path.join(path.dirname(source), `.wrangler-operator-proof-${crypto.randomBytes(8).toString('hex')}.jsonc`)
fs.writeFileSync(target, original.replaceAll(placeholder, id), { flag: 'wx', mode: 0o600 })
process.stdout.write(target)
NODE
)"
pnpm exec wrangler secret put BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET --config "$config_file" <"$secret_file"
echo "Installed encrypted Backroom operator proof binding."
