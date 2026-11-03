import { afterEach, describe, expect, it, vi } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import {
  BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE, BACKROOM_ARTIFACT_SCOPE, TOOL_SCOPE_REQUIREMENTS,
  createBackroomMcpServer,
} from './mcp.server'
import { fetchOfficialV13ControlArtifact } from './v13-official-control.service'

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

const controlId = '11111111-1111-4111-8111-111111111111'
const fixtureId = '22222222-2222-4222-8222-222222222222'
const requestId = '33333333-3333-4333-8333-333333333333'
const image = {
  source_kind: 'canonical_starter_fixture', control_id: controlId,
  request_id: requestId, fixture_id: fixtureId,
  artifact_sha256: 'a'.repeat(64), image_sha256: 'b'.repeat(64), image_id: 'sha256:' + 'c'.repeat(64),
  size_bytes: 123, producer: 'registrar@omniaura.ai', created_at: '2026-10-21T00:00:00Z', verified_at: null,
  image_identity_execution_verified: false, policy_verification_complete: false,
}
function install(response: unknown) {
  vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'synthetic-official-control-admin-token')
  vi.stubEnv('BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET', 'synthetic-official-proof-secret-with-32-bytes')
  vi.stubEnv('DITTO_PLATFORM_API_BASE_URL', 'https://platform.example.test')
  const mock = vi.fn().mockResolvedValue(Response.json(response))
  vi.stubGlobal('fetch', mock)
  return mock
}

describe('official V13 control boundary', () => {
  it('omits retired private-audit writers from the operator catalog', async () => {
    const mock = install({})
    const server = createBackroomMcpServer({ session: { version: 2, uid: 'writer', email: image.producer,
      name: 'Writer', picture: '', accessLevel: 'write', issuedAt: Date.now(), expiresAt: Date.now() + 60_000 },
      scopes: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE, BACKROOM_ARTIFACT_SCOPE], clientName: 'Retirement test' })
    const client = new Client({ name: 'retirement-test', version: '1' })
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])
    try {
      const { tools } = await client.listTools()
      const names = tools.map((tool) => tool.name)
      expect(names).toContain('get_v13_official_controls')
      expect(names).toContain('get_v13_replay_private_statistics')
      for (const name of ['stage_v13_official_control_image', 'verify_v13_official_control_image',
        'record_v13_official_control_approval', 'record_v13_benign_approval',
        'record_v13_replay_private_group', 'register_v13_replay_private_package']) {
        expect(names).not.toContain(name)
        expect((await client.callTool({ name, arguments: {} })).isError).toBe(true)
      }
      expect(mock).not.toHaveBeenCalled()
    } finally { await client.close(); await server.close() }
  })
  it('authenticates the operator for candidate image downloads', async () => {
    const mock = install({ control: { ...image, verified_at: '2026-10-21T00:01:00Z' },
      download_url: 'https://store.example.test/sealed', expires_at: '2026-10-21T00:05:00Z' })
    await fetchOfficialV13ControlArtifact({ controlId }, 'reviewer@omniaura.ai')
    const [url, request] = mock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain(`/${controlId}/artifact`)
    expect(request.method).toBe('GET')
    expect(request.headers).toHaveProperty('X-Admin-Actor', 'reviewer@omniaura.ai')
    expect(request.headers).toHaveProperty('X-Backroom-Operator-Proof')
  })

  it('keeps candidate image downloads behind artifact scope', async () => {
    const mock = install({})
    expect(TOOL_SCOPE_REQUIREMENTS.get('get_v13_official_control_artifact')).toBe(BACKROOM_ARTIFACT_SCOPE)
    const server = createBackroomMcpServer({ session: { version: 2, uid: 'writer', email: image.producer,
      name: 'Writer', picture: '', accessLevel: 'write', issuedAt: Date.now(), expiresAt: Date.now() + 60_000 },
      scopes: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE], clientName: 'Official control artifact test' })
    const client = new Client({ name: 'official-control-artifact-test', version: '1' })
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])
    try {
      expect((await client.callTool({ name: 'get_v13_official_control_artifact', arguments: { controlId } })).isError).toBe(true)
      expect(mock).not.toHaveBeenCalled()
    } finally { await client.close(); await server.close() }
  })
})
