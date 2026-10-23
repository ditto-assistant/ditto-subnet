import { afterEach, expect, it, vi } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { BACKROOM_READ_SCOPE, TOOL_SCOPE_REQUIREMENTS, createBackroomMcpServer } from './mcp.server'
import { fetchAgentSourceReleaseEvidence } from './source-release-evidence.service'

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })
const agentId = '11111111-1111-4111-8111-111111111111'
const candidateAgentId = '22222222-2222-4222-8222-222222222222'
const evidence = {
  agent_id: agentId, artifact_sha256: 'a'.repeat(64), candidate_agent_id: candidateAgentId,
  reference_time: '2026-10-22T16:55:47Z', policy_revision: 8, disclosure: 'public', embargo_hours: 48,
  emission_confirmed_at: null, derived_eligibility_at: null, publication_evidence_at: null,
  publication_proven_by_reference_time: false, exact_sha_public_handoff_count: 0,
  earliest_exact_sha_public_handoff_at: null, absence_proves_nonpublication: false,
  limitations: 'Fail-open audit; absence cannot prove nonpublication',
}

it('offers the timing read without artifact or write scope and strips unexpected private fields', async () => {
  vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'synthetic-source-evidence-token')
  vi.stubEnv('DITTO_PLATFORM_API_BASE_URL', 'https://platform.example.test')
  const mock = vi.fn().mockResolvedValue(Response.json({ ...evidence, source_ip: 'private', download_url: 'private' }))
  vi.stubGlobal('fetch', mock)
  const server = createBackroomMcpServer({ session: { version: 2, uid: 'reader', email: 'reader@omniaura.ai',
    name: 'Reader', picture: '', accessLevel: 'read', issuedAt: Date.now(), expiresAt: Date.now() + 60_000 },
    scopes: [BACKROOM_READ_SCOPE], clientName: 'Source evidence test' })
  const client = new Client({ name: 'source-evidence-test', version: '1' })
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
  await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])
  try {
    expect(TOOL_SCOPE_REQUIREMENTS.get('get_agent_source_release_evidence')).toBe(BACKROOM_READ_SCOPE)
    const result = await client.callTool({ name: 'get_agent_source_release_evidence', arguments: { agentId, candidateAgentId } })
    expect(result.isError).not.toBe(true)
    expect(JSON.stringify(result)).not.toContain('private')
    expect(mock.mock.calls[0][0]).toContain(`/agents/${agentId}/source-release-evidence?candidate_agent_id=${candidateAgentId}`)
    expect(mock.mock.calls[0][1].method).toBe('GET')
  } finally { await client.close(); await server.close() }
})

it('rejects path injection before requesting Platform', async () => {
  const mock = vi.fn()
  vi.stubGlobal('fetch', mock)
  await expect(fetchAgentSourceReleaseEvidence({ agentId, candidateAgentId: '../secret' })).rejects.toThrow()
  expect(mock).not.toHaveBeenCalled()
})
