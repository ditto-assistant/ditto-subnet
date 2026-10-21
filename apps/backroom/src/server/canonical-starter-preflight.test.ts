import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchCanonicalStarterPreflight } from './admin.service'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe('canonical starter operator-proof preflight', () => {
  const fixture = {
    release: 'v0.330.5',
    release_commit: '940304019aeec55e7b473bc163a51851e99db907',
    source_tree: '9ffd5370e21bbe3135f1ee830b7b68723950619b',
    archive_sha256: 'a'.repeat(64),
    archive_size_bytes: 123,
    fixture: {
      canary_id: '11111111-1111-4111-8111-111111111111',
      request_id: '22222222-2222-4222-8222-222222222222',
      agent_id: null,
      source_attempt_id: null,
      source_kind: 'canonical_starter_fixture',
      artifact_sha256: 'a'.repeat(64),
      target_node_id: 'node-1',
      expected_agent_status: null,
      expected_score_count: null,
      review_label: 'candidate_clear',
      run_mode: 'source_only',
      status: 'ready',
      claimed_instance_id: null,
      report: null,
      error_code: null,
      created_at: '2026-10-21T00:00:00Z',
      completed_at: null,
    },
    stored_object_matches: true,
    fixture_capable_worker_ready: true,
    can_schedule: true,
  }

  it.each([
    { platform: true, backroom: true, ready: true },
    { platform: true, backroom: false, ready: false },
    { platform: false, backroom: true, ready: false },
    { platform: undefined, backroom: true, ready: false },
  ])('reports presence without claiming matching secrets: %j', async ({ platform, backroom, ready }) => {
    const secret = 'synthetic-proof-value-with-at-least-32-characters'
    vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'synthetic-admin-token')
    vi.stubEnv('BACKROOM_PLATFORM_OPERATOR_PROOF_SECRET', backroom ? secret : '')
    const fetchMock = vi.fn().mockResolvedValue(Response.json({
      ...fixture,
      ...(platform === undefined ? {} : { operator_proof_configured: platform }),
    }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchCanonicalStarterPreflight()
    expect(result.operator_proof_configured).toBe(platform ?? null)
    expect(result.backroom_operator_proof_configured).toBe(backroom)
    expect(result.operator_proof_match).toBe('not_verified')
    expect(result.can_schedule).toBe(ready)
    expect(JSON.stringify(result)).not.toContain(secret)
    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, request] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/fixture/preflight')
    expect(request.method).toBe('GET')
    expect(request.body).toBeUndefined()
    expect(request.headers).not.toHaveProperty('X-Backroom-Operator-Proof')
  })
})
