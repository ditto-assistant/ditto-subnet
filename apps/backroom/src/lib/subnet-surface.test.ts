import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const appRoot = join(import.meta.dirname, '..', '..')

describe('public subnet Backroom boundary', () => {
  it('does not ship Ditto product-control routes', () => {
    const routes = readdirSync(join(appRoot, 'src', 'routes', '_authenticated'))
    expect(routes).not.toEqual(
      expect.arrayContaining([
        'agent-access.tsx',
        'airdrop.tsx',
        'app-reviews.tsx',
        'feature-flags.tsx',
        'ops-logs.tsx',
      ]),
    )
  })

  it('does not expose product server functions', () => {
    const functions = readFileSync(
      join(appRoot, 'src', 'server', 'admin.functions.ts'),
      'utf8',
    )

    for (const forbidden of [
      'updateFeatureFlag',
      'setFeatureFlagOverride',
      'listAppReviews',
      'queueProductionAirdrop',
      'listOpsLogEntries',
    ]) {
      expect(functions).not.toContain(forbidden)
    }
  })

  // The MCP transport is no longer excluded: SN118 is operated from
  // backroom.dittobench.ai, so its tools have to live here. What stays excluded
  // is the Ditto app surface — feature flags and app reviews reach the private
  // product API this deployment is forbidden to hold credentials for, so their
  // absence is a boundary, not an oversight, and this guard is what keeps a
  // later port from quietly dragging them across.
  it('serves the subnet MCP without the Ditto app tools', () => {
    const server = readFileSync(join(appRoot, 'src', 'server.ts'), 'utf8')
    const tools = readFileSync(join(appRoot, 'src', 'server', 'mcp.server.ts'), 'utf8')

    expect(server).toContain("apiRoute: '/mcp'")
    expect(tools).toContain("'get_burn_settings'")

    for (const forbidden of [
      'list_feature_flags',
      'update_feature_flag',
      'set_feature_flag_override',
      'delete_feature_flag_override',
      'apply_feature_flag_override_batch',
      'list_app_reviews',
      'approve_app',
      'reject_app',
    ]) {
      expect(tools).not.toContain(forbidden)
    }
  })

  it('authenticates independently of the private Ditto app', () => {
    const sourceFiles = [
      'src/server/oauth.server.ts',
      'src/server/session.server.ts',
      'src/server/ditto.server.ts',
      // The MCP was ported from a deployment whose grants carried a Firebase
      // token and called the private Ditto API. Nothing about the port is
      // finished if that coupling came across with it.
      'src/server.ts',
      'src/server/mcp.server.ts',
      'src/server/mcp-oauth.server.ts',
      'src/server/mcp-handler.server.ts',
    ].map((path) => readFileSync(join(appRoot, path), 'utf8'))
    const runtime = readFileSync(join(appRoot, 'wrangler.jsonc'), 'utf8')
    const source = [...sourceFiles, runtime].join('\n')

    for (const forbidden of [
      'FIREBASE_API_KEY',
      'firebaseIdToken',
      'DITTO_API_BASE_URL',
      '/api/v5/admin/backroom-access',
      'https://api.heyditto.ai',
    ]) {
      expect(source).not.toContain(forbidden)
    }
    expect(source).toContain('BACKROOM_ADMIN_EMAILS')
    expect(source).toContain('DITTO_PLATFORM_API_BASE_URL')
  })

  // This repository is public and the OAuth KV namespace holds live operator
  // grants plus the access and refresh tokens of everyone with backroom:write.
  // The id is not itself a credential, but committing it turns "holds some
  // token for this account" into "is one command from the production token
  // store", and points a stray `wrangler dev --remote` at real grants. It is
  // injected at deploy time instead, like the Cloudflare account id and token.
  it('never commits a real Cloudflare namespace id', () => {
    const runtime = readFileSync(join(appRoot, 'wrangler.jsonc'), 'utf8')

    expect(runtime).toContain('OAUTH_KV_NAMESPACE_ID_INJECTED_AT_DEPLOY')
    // Cloudflare namespace ids are 32 lowercase hex characters.
    expect(runtime).not.toMatch(/\b[0-9a-f]{32}\b/)
  })
})
