import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchUploadPaymentProof } from './upload-payment-proof.service'

vi.mock('@tanstack/react-start/server-only', () => ({}))

const blockHash = '0x3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f'
const funded = {
  block_hash: blockHash, extrinsic_index: 12, state: 'funded_agent',
  miner_hotkey: '5MinerHotkey',
  miner_coldkey: '5PaymentColdkey',
  amount_rao: 100_000_000, dest_address: '5UploadFeeAddress',
  block_timestamp: '2026-11-06T09:38:36Z', recorded_at: '2026-11-06T11:23:07.017787Z',
  agent: {
    agent_id: '00000000-0000-4000-8000-000000000034', agent_name: 'agent', agent_version: 34,
    agent_status: 'scored', artifact_sha256: 'a'.repeat(64), submitted_at: '2026-11-06T11:23:07.017787Z',
  },
  generator_submission_id: null, recovery_expires_at: null, recovery_expired: null,
  generated_at: '2026-11-11T22:00:00Z',
}

describe('fetchUploadPaymentProof', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs() })

  it('reads one proof by lowercased hash and index', async () => {
    vi.stubEnv('DITTO_PLATFORM_API_BASE_URL', 'https://platform.example.test')
    vi.stubEnv('DITTO_ADMIN_API_TOKEN', 'synthetic-payment-proof-token')
    const mock = vi.fn().mockResolvedValue(Response.json(funded))
    vi.stubGlobal('fetch', mock)

    const result = await fetchUploadPaymentProof({
      blockHash: `0x${blockHash.slice(2).toUpperCase()}`, extrinsicIndex: 12,
    })

    const url = new URL(String(mock.mock.calls[0][0]))
    expect(url.pathname).toBe(`/api/v1/admin/upload-payments/${blockHash}/12`)
    expect(result.state).toBe('funded_agent')
    expect(result.agent?.agent_id).toBe(funded.agent.agent_id)
  })

  it('refuses a malformed proof before calling Platform', async () => {
    const mock = vi.fn()
    vi.stubGlobal('fetch', mock)

    await expect(fetchUploadPaymentProof({ blockHash: '0x1234', extrinsicIndex: 12 })).rejects.toThrow()
    await expect(fetchUploadPaymentProof({ blockHash, extrinsicIndex: -1 })).rejects.toThrow()
    expect(mock).not.toHaveBeenCalled()
  })
})
