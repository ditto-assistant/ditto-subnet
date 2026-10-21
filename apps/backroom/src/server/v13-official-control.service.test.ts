import { afterEach, describe, expect, it, vi } from 'vitest'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import {
  BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE, TOOL_SCOPE_REQUIREMENTS,
  createBackroomMcpServer,
} from './mcp.server'
import { stageOfficialV13ControlImage, verifyOfficialV13ControlImage, recordOfficialV13ControlApproval } from './v13-official-control.service'

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
const stage = { requestId, fixtureId, imageSha256: image.image_sha256, imageId: image.image_id,
  sizeBytes: 123, confirmation: 'STAGE OFFICIAL V13 CONTROL IMAGE' }
const verify = { controlId, imageSha256: image.image_sha256, imageId: image.image_id,
  confirmation: 'VERIFY OFFICIAL V13 CONTROL IMAGE' }
const approval = { controlId, imageSha256: image.image_sha256, imageId: image.image_id,
  profileSha256: 'd'.repeat(64), reviewEvidenceSha256: 'e'.repeat(64),
  reason: 'Independent exact-image report-only review', confirmation: 'RECORD OFFICIAL V13 CONTROL APPROVAL' }

function install(response: unknown) {
  vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'synthetic-official-control-admin-token')
  vi.stubEnv('BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET', 'synthetic-official-proof-secret-with-32-bytes')
  vi.stubEnv('DITTO_PLATFORM_API_BASE_URL', 'https://platform.example.test')
  const mock = vi.fn().mockResolvedValue(Response.json(response))
  vi.stubGlobal('fetch', mock)
  return mock
}

describe('official V13 control boundary', () => {
  it('binds upload, verify and approval to the signed operator without fabricating miner IDs', async () => {
    const mock = install({ control: image, upload_url: 'https://store.example.test/staging',
      required_headers: { 'Content-Length': '123' }, expires_at: '2026-10-21T00:05:00Z' })
    await stageOfficialV13ControlImage(stage, image.producer)
    let [url, request] = mock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/v13-official-controls/images')
    expect(request.headers).toHaveProperty('X-Backroom-Operator-Proof')
    expect(request.headers).toHaveProperty('X-Admin-Actor', image.producer)
    expect(JSON.parse(request.body as string)).toEqual({ request_id: requestId, fixture_id: fixtureId,
      image_sha256: image.image_sha256, image_id: image.image_id, size_bytes: 123 })
    expect(request.body).not.toContain('agent_id')
    expect(request.body).not.toContain('attempt_id')
    mock.mockResolvedValueOnce(Response.json({ ...image, verified_at: '2026-10-21T00:01:00Z' }))
    await verifyOfficialV13ControlImage(verify, image.producer)
    ;[url, request] = mock.mock.calls[1] as [string, RequestInit]
    expect(url).toContain(`/${controlId}/verify`)
    expect(JSON.parse(request.body as string)).toEqual({ image_sha256: image.image_sha256, image_id: image.image_id })
    const recorded = { source_kind: image.source_kind, approval_id: '44444444-4444-4444-8444-444444444444',
      control_id: controlId, fixture_id: fixtureId, artifact_sha256: image.artifact_sha256,
      image_sha256: image.image_sha256, image_id: image.image_id, profile_sha256: approval.profileSha256,
      review_evidence_sha256: approval.reviewEvidenceSha256, approval_receipt_sha256: 'f'.repeat(64),
      actor: 'reviewer@omniaura.ai', reason: approval.reason, approved_at: '2026-10-21T00:02:00Z',
      status: 'recorded_unverified', policy_verification_complete: false }
    mock.mockResolvedValueOnce(Response.json(recorded))
    expect(await recordOfficialV13ControlApproval(approval, recorded.actor)).toEqual(recorded)
    expect((mock.mock.calls[2] as [string, RequestInit])[1].headers).toHaveProperty('X-Admin-Actor', recorded.actor)
  })

  it.each([
    ['stage_v13_official_control_image', stage],
    ['verify_v13_official_control_image', verify],
    ['record_v13_official_control_approval', approval],
  ])('requires live write access for %s', async (name, input) => {
    const fetchMock = install({})
    expect(TOOL_SCOPE_REQUIREMENTS.get(name as string)).toBe(BACKROOM_WRITE_SCOPE)
    const server = createBackroomMcpServer({ session: { version: 2, uid: 'reader', email: 'reader@omniaura.ai',
      name: 'Reader', picture: '', accessLevel: 'read', issuedAt: Date.now(), expiresAt: Date.now() + 60_000 },
      scopes: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE], clientName: 'Official control test' })
    const client = new Client({ name: 'official-control-test', version: '1' })
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair()
    await Promise.all([server.connect(serverTransport), client.connect(clientTransport)])
    try {
      const result = await client.callTool({ name: name as string, arguments: input as Record<string, unknown> })
      expect(result.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally { await client.close(); await server.close() }
  })

  it('refuses invalid confirmation before making an upload request', async () => {
    const mock = install({})
    await expect(stageOfficialV13ControlImage({ ...stage, confirmation: 'CLEAR' }, image.producer)).rejects.toThrow()
    expect(mock).not.toHaveBeenCalled()
  })
})
