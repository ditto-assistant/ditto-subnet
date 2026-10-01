import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchTreasuryLedgerReadiness } from './admin.service'

const originalToken = process.env.DITTO_ADMIN_API_TOKEN
const observation = {
  configured_proposal: null,
  observer_status: 'disabled',
  observer_scope: 'this_platform_process',
  latest_stored_epoch_index: null,
  latest_stored_ledger_digest: null,
  stored_shadow_pin: null,
  blocking_reasons: ['producer_disabled', 'no_epoch_pin', 'shadow_only'],
  offline_policy_verified: false,
  weight_effect: 'none',
  can_enforce_weights: false,
}

afterEach(() => {
  vi.unstubAllGlobals()
  if (originalToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalToken
})

describe('treasury ledger observation boundary', () => {
  it('reads stored evidence and blockers without a write or chain request', async () => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    const fetchMock = vi.fn().mockResolvedValue(Response.json(observation))
    vi.stubGlobal('fetch', fetchMock)
    expect(await fetchTreasuryLedgerReadiness()).toEqual(observation)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toMatch(/\/api\/v1\/admin\/treasury-settings\/ledger-readiness$/)
    expect(init.method ?? 'GET').toBe('GET')
    expect(init.body).toBeUndefined()
  })

  it.each([
    { offline_policy_verified: true },
    { can_enforce_weights: true },
    { weight_effect: 'active' },
  ])('refuses an API observation upgraded to funding authority: %j', async (change) => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ ...observation, ...change })))
    await expect(fetchTreasuryLedgerReadiness()).rejects.toThrow()
  })
})
