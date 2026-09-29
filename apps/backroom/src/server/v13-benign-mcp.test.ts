import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { BackroomSession } from '../lib/auth.types'
import {
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  createBackroomMcpServer,
} from './mcp.server'

const id = '11111111-1111-4111-8111-111111111111'
const session: BackroomSession = {
  version: 2,
  uid: 'google-generator',
  email: 'generator@omniaura.ai',
  name: 'Generator',
  picture: '',
  accessLevel: 'write',
  issuedAt: Date.now(),
  expiresAt: Date.now() + 60_000,
}
const approval = {
  approval_id: id,
  agent_id: id,
  attempt_id: id,
  artifact_sha256: 'a'.repeat(64),
  image_sha256: 'b'.repeat(64),
  profile_sha256: 'c'.repeat(64),
  review_evidence_sha256: 'd'.repeat(64),
  approval_receipt_sha256: 'e'.repeat(64),
  actor: 'legacy-label',
  reason: 'benign source review',
  approved_at: '2026-09-29T00:00:00Z',
  status: 'recorded_unverified',
}
const group = {
  group_id: id,
  replay_id: id,
  target_agent_id: id,
  target_attempt_id: id,
  target_artifact_sha256: '1'.repeat(64),
  target_image_sha256: '2'.repeat(64),
  control_agent_id: id,
  control_attempt_id: id,
  control_artifact_sha256: 'a'.repeat(64),
  control_image_sha256: 'b'.repeat(64),
  approval_id: id,
  approval_receipt_sha256: 'e'.repeat(64),
  profile_sha256: 'c'.repeat(64),
  target_receipt_sha256: 'f'.repeat(64),
  control_receipt_sha256: '0'.repeat(64),
  actor: session.email,
  started_at: '2026-09-29T00:00:01Z',
  status: 'recorded_unverified',
}

beforeEach(() => {
  vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'test-platform-token')
  vi.stubEnv('DITTO_V13_BENIGN_ATTESTATION_SECRET', 's'.repeat(48))
  vi.stubEnv('BACKROOM_ADMIN_EMAILS', session.email)
  vi.stubEnv('BACKROOM_BLOCKED_EMAILS', '')
})
afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

async function connect(scopes: string[], principal = session) {
  const server = createBackroomMcpServer({
    session: principal,
    scopes,
    clientName: 'Test',
  })
  const client = new Client({ name: 'test', version: '1' })
  const [a, b] = InMemoryTransport.createLinkedPair()
  await Promise.all([client.connect(a), server.connect(b)])
  return { client, server }
}

const operations = [
  [
    'attest_v13_benign_approval',
    'ATTEST V13 BENIGN CONTROL',
    'attest-known-benign',
  ],
  [
    'record_v13_private_generation_group',
    'RECORD V13 GENERATION',
    'authorize-generation',
  ],
  [
    'record_v13_replay_private_group',
    'RECORD V13 REPLAY GENERATION',
    'authorize-generation',
  ],
] as const

it.each(operations)(
  'uses the signed-in principal for %s and returns no assertion',
  async (name, confirmation, action) => {
    const fetchMock = vi.fn(
      async (_url: string, options?: RequestInit) =>
        new Response(
          JSON.stringify(
            options?.method === 'POST' && action === 'authorize-generation'
              ? group
              : approval,
          ),
          { status: 200, headers: { 'content-type': 'application/json' } },
        ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([
      BACKROOM_READ_SCOPE,
      BACKROOM_WRITE_SCOPE,
    ])
    try {
      const response = await client.callTool({
        name,
        arguments: {
          approvalId: id,
          reason: 'reviewed exact source and behavior',
          confirmation,
          replayId: id,
          targetAgentId: id,
          targetAttemptId: id,
          targetArtifactSha256: '1'.repeat(64),
          targetImageSha256: '2'.repeat(64),
          profileSha256: 'c'.repeat(64),
          sub: 'spoofed',
          email: 'spoofed@omniaura.ai',
          assertion: 'spoofed',
        },
      })
      expect(response.isError).not.toBe(true)
      const post = fetchMock.mock.calls.find(
        ([, options]) => options?.method === 'POST',
      )
      expect(post).toBeDefined()
      const body = JSON.parse(String(post![1]!.body))
      const assertion = body.assertion ?? body.generator_assertion
      const claims = JSON.parse(
        Buffer.from(assertion.split('.')[1], 'base64url').toString(),
      )
      expect(claims).toMatchObject({
        sub: session.uid,
        email: session.email,
        action,
        approval_id: id,
        evidence_sha256: approval.review_evidence_sha256,
      })
      expect(new Headers(post![1]!.headers).get('X-Admin-Actor')).toBe(
        session.email,
      )
      expect(JSON.stringify(response)).not.toContain(assertion)
      expect(JSON.stringify(response)).not.toContain('s'.repeat(48))
    } finally {
      await client.close()
      await server.close()
    }
  },
)

it.each(operations)(
  'blocks %s without write scope',
  async (name, confirmation) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    const { client, server } = await connect([BACKROOM_READ_SCOPE])
    try {
      const response = await client.callTool({
        name,
        arguments: {
          approvalId: id,
          reason: 'reviewed exact source and behavior',
          confirmation,
          replayId: id,
          targetAgentId: id,
          targetAttemptId: id,
          targetArtifactSha256: '1'.repeat(64),
          targetImageSha256: '2'.repeat(64),
          profileSha256: 'c'.repeat(64),
        },
      })
      expect(response.isError).toBe(true)
      expect(fetchMock).not.toHaveBeenCalled()
    } finally {
      await client.close()
      await server.close()
    }
  },
)
