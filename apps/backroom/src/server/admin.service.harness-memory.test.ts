import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchAgentHarnessMemory, fetchHarnessMemoryList } from './admin.service'

const MIB = 1 << 20
const agentId = '0b6f3d4e-8a2c-4f1e-9d7b-5c3a2e1f0a9b'

const rollup = {
  agent_id: agentId,
  agent_name: 'heavy-agent',
  miner_hotkey: '5' + 'M'.repeat(47),
  runs: 3,
  scored_runs: 2,
  failed_runs: 1,
  oom_runs: 1,
  peak_bytes_max: 2048 * MIB,
  p50_bytes_median: 900 * MIB,
  p95_bytes_median: 1800 * MIB,
  p95_bytes_max: 2040 * MIB,
  limit_bytes_max: 2048 * MIB,
  last_recorded_at: '2026-11-22T18:00:00Z',
}

describe('SN-500 harness memory reads', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    delete process.env.DITTO_ADMIN_API_TOKEN
  })

  it('pages the operator listing and keeps byte-exact rollups', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        generated_at: '2026-11-22T18:01:00Z',
        agents: [rollup],
        offset: 50,
        has_more: true,
        note: 'runs before it have no memory data',
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const parsed = await fetchHarnessMemoryList(500, 50)

    const requestUrl = new URL((fetchMock.mock.calls[0] as [string])[0])
    expect(requestUrl.pathname).toBe('/api/v1/admin/harness-memory')
    expect(requestUrl.searchParams.get('limit')).toBe('200')
    expect(requestUrl.searchParams.get('offset')).toBe('50')
    expect(parsed.agents[0].p95_bytes_max).toBe(2040 * MIB)
    expect(parsed.has_more).toBe(true)
  })

  it('reads one agent and reports an unmeasured agent as a null rollup', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({ agent_id: agentId, rollup: null, runs: [] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const parsed = await fetchAgentHarnessMemory(agentId, 10)

    const requestUrl = new URL((fetchMock.mock.calls[0] as [string])[0])
    expect(requestUrl.pathname).toBe(`/api/v1/admin/agents/${agentId}/harness-memory`)
    expect(requestUrl.searchParams.get('limit')).toBe('10')
    expect(parsed.rollup).toBeNull()
    expect(parsed.runs).toEqual([])
  })
})
