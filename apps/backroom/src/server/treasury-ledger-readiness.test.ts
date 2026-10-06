import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchTreasuryLedgerReadiness } from './admin.service'

const originalToken = process.env.DITTO_ADMIN_API_TOKEN
const observation = {
  configured_proposal: null,
  proposal_approval_status: 'not_configured',
  proposal_approved_policy_digest: null,
  observer_status: 'disabled',
  observer_scope: 'this_platform_process',
  latest_stored_epoch_index: null,
  latest_stored_ledger_digest: null,
  stored_shadow_pin: null,
  stored_enforcing_pin: null,
  enforcement_configured: false,
  fleet_gate: 'not_checked',
  blocking_reasons: ['producer_disabled', 'no_epoch_pin', 'shadow_only'],
  offline_policy_verified: false,
  offline_epoch_verified: false,
  weight_effect: 'none',
  can_enforce_weights: false,
  ledger_schedule_probe_status: 'not_checked',
  ledger_schedule_probe_epoch: null,
  ledger_schedule_probe_block: null,
  ledger_schedule_matches_stored_pin: null,
  ledger_schedule_failure_kind: null,
}

afterEach(() => {
  vi.unstubAllGlobals()
  if (originalToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalToken
})

describe('treasury ledger observation boundary', () => {
  it('preserves verified enforcing diagnostics without turning them into spending authority', async () => {
    const pin = JSON.parse(readFileSync(new URL(
      '../../../../packages/ditto-screening-protocol/tests/fixtures/treasury_enforcing_pin_v2.json',
      import.meta.url,
    ), 'utf8'))
    const ready = {
      ...observation,
      configured_proposal: pin.policy,
      proposal_approval_status: 'verified',
      proposal_approved_policy_digest: pin.policy_digest,
      stored_enforcing_pin: pin,
      enforcement_configured: true,
      fleet_gate: 'ready',
      offline_epoch_verified: true,
      can_enforce_weights: true,
      blocking_reasons: [],
    }
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(ready)))
    expect(await fetchTreasuryLedgerReadiness()).toEqual(ready)
    for (const change of [
      { enforcement_configured: false }, { stored_enforcing_pin: null },
      { fleet_gate: 'not_ready' }, { offline_epoch_verified: false },
      { blocking_reasons: ['enforcing_pin_unverified'] },
    ]) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ ...ready, ...change })))
      await expect(fetchTreasuryLedgerReadiness()).rejects.toThrow()
    }
    expect(ready.offline_policy_verified).toBe(false)
    expect(ready.weight_effect).toBe('none')
  })
  it('defaults missing new proposal-proof fields without creating authority', async () => {
    const legacy: Partial<typeof observation> = { ...observation }
    delete legacy.proposal_approval_status
    delete legacy.proposal_approved_policy_digest
    delete legacy.ledger_schedule_probe_status
    delete legacy.ledger_schedule_probe_epoch
    delete legacy.ledger_schedule_probe_block
    delete legacy.ledger_schedule_matches_stored_pin
    delete legacy.ledger_schedule_failure_kind
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(legacy)))
    expect(await fetchTreasuryLedgerReadiness()).toEqual(observation)
  })
  it('preserves bounded schedule and authority failures through the public service', async () => {
    const diagnostic = {
      ...observation,
      ledger_schedule_probe_status: 'unavailable',
      ledger_schedule_failure_kind: 'timeout',
      validation_failure_stage: 'setter_roster',
      validation_failure_step: 'permit_vector',
      validation_failure_kind: 'timeout',
    }
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ ...diagnostic, future_secret: 'never publish' })))
    expect(await fetchTreasuryLedgerReadiness()).toEqual(diagnostic)
  })
  it('preserves known shadow evidence and ignores future fields at every level', async () => {
    const pin = JSON.parse(readFileSync(new URL(
      '../../../../packages/ditto-screening-protocol/tests/fixtures/treasury_ledger_pin_v1.json',
      import.meta.url,
    ), 'utf8'))
    const expected = {
      ...observation,
      configured_proposal: pin.policy,
      observer_status: 'observed',
      latest_stored_epoch_index: 7,
      latest_stored_ledger_digest: 'a'.repeat(64),
      stored_shadow_pin: pin,
      blocking_reasons: ['shadow_only'],
    }
    const futurePolicy = { ...pin.policy, future: true,
      buckets: pin.policy.buckets.map((bucket: object) => ({ ...bucket, future: true })) }
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({
      ...expected,
      future: true,
      configured_proposal: futurePolicy,
      stored_shadow_pin: { ...pin, future: true, policy: futurePolicy,
        identity: { ...pin.identity, future: true } },
    })))
    expect(await fetchTreasuryLedgerReadiness()).toEqual(expected)
  })

  it('shows proposal approval distinctly while epoch and funding authority stay false', async () => {
    const pin = JSON.parse(readFileSync(new URL(
      '../../../../packages/ditto-screening-protocol/tests/fixtures/treasury_ledger_pin_v1.json',
      import.meta.url,
    ), 'utf8'))
    const response = {
      ...observation,
      configured_proposal: pin.policy,
      proposal_approval_status: 'verified',
      proposal_approved_policy_digest: pin.policy_digest,
    }
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json(response)))
    expect(await fetchTreasuryLedgerReadiness()).toEqual(response)
  })

  it.each([
    { configured_proposal: {} },
    { stored_shadow_pin: { mode: 'active' } },
    { observer_status: 'future_state' },
    { ledger_schedule_probe_status: 'future_state' },
    { ledger_schedule_failure_kind: 'provider secret' },
    { validation_failure_step: 'provider secret' },
    { blocking_reasons: ['unbounded_new_reason'] },
    { proposal_approval_status: 'verified' },
    { proposal_approved_policy_digest: 'a'.repeat(64) },
    { proposal_approval_status: 'verified', proposal_approved_policy_digest: 'a'.repeat(64) },
  ])('refuses malformed known observation fields: %j', async (change) => {
    process.env.DITTO_ADMIN_API_TOKEN = 'synthetic-test-token'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ ...observation, ...change })))
    await expect(fetchTreasuryLedgerReadiness()).rejects.toThrow()
  })

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
import { readFileSync } from 'node:fs'
