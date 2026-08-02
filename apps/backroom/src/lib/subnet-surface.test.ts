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

  it('does not expose product server functions or MCP transport', () => {
    const functions = readFileSync(
      join(appRoot, 'src', 'server', 'admin.functions.ts'),
      'utf8',
    )
    const server = readFileSync(join(appRoot, 'src', 'server.ts'), 'utf8')

    for (const forbidden of [
      'updateFeatureFlag',
      'setFeatureFlagOverride',
      'listAppReviews',
      'queueProductionAirdrop',
      'listOpsLogEntries',
    ]) {
      expect(functions).not.toContain(forbidden)
    }
    expect(server).not.toContain("apiRoute: '/mcp'")
  })

  it('authenticates independently of the private Ditto app', () => {
    const sourceFiles = [
      'src/server/oauth.server.ts',
      'src/server/session.server.ts',
      'src/server/ditto.server.ts',
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
})
