import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchArtifactFetchAudit } from './artifact-fetch-audit.service'

vi.mock('@tanstack/react-start/server-only', () => ({}))

const sha = 'a'.repeat(64)
const row = {
  fetched_at: '2026-10-30T00:00:00Z', agent_id: '00000000-0000-4000-8000-000000000001',
  artifact_sha256: sha, endpoint: 'validator.agent_artifact', requester_kind: 'validator',
  requester_id: '5F' + 'x'.repeat(46), requester_instance_id: null, lease_id: null, bench_version: 13,
  served_source: true, served_screened_image: true, source_ip_hash: '0123456789abcdef',
  forwarded_claim_present: true,
}
const audit = {
  agent_ids: [row.agent_id], artifact_sha256: sha, before: '2026-10-30T01:00:00Z', total: 1,
  truncated: false, rows: [{ ...row, source_ip: '203.0.113.9', detail: { claimed_forwarded_for: { 'x-real-ip': '198.51.100.2' } } }],
  requesters: [{ requester_kind: 'validator', requester_id: row.requester_id, fetch_count: 1,
    source_fetch_count: 1, first_fetched_at: row.fetched_at, last_fetched_at: row.fetched_at }],
  absence_proves_no_fetch: false, limitations: 'fail-open',
}

describe('fetchArtifactFetchAudit', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

  it('queries by sha with a reference time and never returns raw addresses', async () => {
    vi.stubEnv('DITTO_PLATFORM_API_BASE_URL', 'https://platform.example.test')
    vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'synthetic-fetch-audit-token')
    const mock = vi.fn().mockResolvedValue(Response.json(audit))
    vi.stubGlobal('fetch', mock)

    const result = await fetchArtifactFetchAudit({ sha, before: '2026-10-30T01:00:00Z' })

    const url = new URL(String(mock.mock.calls[0][0]))
    expect(url.pathname).toBe('/api/v1/admin/artifact-fetch-audit')
    expect(url.searchParams.get('artifact_sha256')).toBe(sha)
    expect(url.searchParams.get('before')).toBe('2026-10-30T01:00:00Z')
    expect(url.searchParams.has('agent_id')).toBe(false)
    const text = JSON.stringify(result)
    expect(text).not.toContain('203.0.113.9')
    expect(text).not.toContain('198.51.100.2')
    await expect(fetchArtifactFetchAudit({ sha, agentId: row.agent_id })).rejects.toThrow()
  })
})
