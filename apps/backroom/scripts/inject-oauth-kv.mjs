#!/usr/bin/env node
/**
 * Bind the production OAuth KV namespace immediately before `wrangler deploy`.
 *
 * The namespace id stays out of the repository for the same reason the
 * Cloudflare account id and API token do (see
 * `infra/terraform/stacks/cloudflare-dittobench/README.md`): this is a public
 * repository, and that namespace holds live OAuth grants plus the access and
 * refresh tokens of every operator with `backroom:write`. The id is not a
 * credential — reading it requires an account-scoped token — but publishing it
 * turns "holds some token for this account" into "is one command away from the
 * production token store", and makes a stray `wrangler dev --remote` from a
 * local checkout point at real grants.
 *
 * Leaving the placeholder in the committed config is therefore load-bearing:
 * it is not a valid namespace id, so any deploy that skips this step fails at
 * Cloudflare rather than silently shipping a Worker whose OAuth provider has
 * nowhere to persist a grant.
 *
 * Terraform owns the namespace itself and exports
 * `backroom_oauth_kv_namespace_id`; that value belongs in the `prod`
 * environment as the `BACKROOM_OAUTH_KV_ID` variable.
 *
 * Wrangler cannot do this on its own: as of 4.110 it performs no environment
 * substitution inside `wrangler.jsonc` (a literal `${VAR}` is passed through as
 * the binding id), and `wrangler deploy` has no namespace-id flag.
 */

import { readFileSync, writeFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const PLACEHOLDER = 'OAUTH_KV_NAMESPACE_ID_INJECTED_AT_DEPLOY'
const configPath = join(dirname(fileURLToPath(import.meta.url)), '..', 'wrangler.jsonc')

function fail(message) {
  console.error(`inject-oauth-kv: ${message}`)
  process.exit(1)
}

const namespaceId = (process.env.BACKROOM_OAUTH_KV_ID ?? '').trim()
if (!namespaceId) {
  fail(
    'BACKROOM_OAUTH_KV_ID is empty. Set it in the prod environment from the ' +
      'cloudflare-dittobench Terraform output `backroom_oauth_kv_namespace_id`.',
  )
}
// Cloudflare namespace ids are 32 lowercase hex characters. Checking the shape
// here turns a typo into a failed deploy step instead of a Worker that deploys
// and then cannot answer a single OAuth request.
if (!/^[0-9a-f]{32}$/.test(namespaceId)) {
  fail(
    `BACKROOM_OAUTH_KV_ID is not a Cloudflare namespace id (expected 32 hex characters, got ${namespaceId.length}).`,
  )
}

const config = readFileSync(configPath, 'utf8')
if (!config.includes(PLACEHOLDER)) {
  fail(
    `${configPath} no longer contains ${PLACEHOLDER}. Either the OAUTH_KV ` +
      'binding was removed, or a real namespace id was committed — which is ' +
      'the thing this script exists to avoid.',
  )
}

writeFileSync(configPath, config.replaceAll(PLACEHOLDER, namespaceId))
console.log(`inject-oauth-kv: bound OAUTH_KV to ${namespaceId.slice(0, 6)}…`)
