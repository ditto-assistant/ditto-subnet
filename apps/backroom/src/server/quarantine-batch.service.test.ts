import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { executeScreeningQuarantineBatch } from './admin.service'
import { PlatformAdminError } from './ditto.server'

// #3042: Platform applies a batch decision by decision, so its answer can be
// lost after it has committed some or all of them.

const ids = [
  '11111111-1111-4111-8111-111111111111',
  '22222222-2222-4222-8222-222222222222',
  '33333333-3333-4333-8333-333333333333',
]
const decisions = ids.map((quarantineId, index) => ({
  quarantineId,
  expectedAgentId: `aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa${index}`,
  expectedArtifactSha256: 'ab'.repeat(32),
  resolution: 'release' as const,
  reason: 'Manually reviewed source; the flagged path is general routing',
}))
const input = {
  decisions,
  previewToken: '1234567890.abcdefghijklmnopqrstuvwxyz1234567890abcdef',
  confirmed: true as const,
}

function previewItem(quarantineId: string, disposition: string, message: string) {
  return {
    quarantine_id: quarantineId,
    agent_id: null,
    agent_name: null,
    artifact_sha256: 'ab'.repeat(32),
    resolution: 'release',
    reason: decisions[0].reason,
    disposition,
    resulting_agent_status: disposition === 'already_applied' ? 'evaluating' : null,
    public_reason_code: null,
    public_record_hash: null,
    terminal_reconciliation: false,
    terminal_ruling: null,
    message,
  }
}

const timeout = () => Promise.reject(new DOMException('aborted due to timeout', 'TimeoutError'))

describe('screening quarantine batch execution', () => {
  beforeEach(() => {
    process.env.DITTO_ADMIN_API_TOKEN = 'secret'
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete process.env.DITTO_ADMIN_API_TOKEN
  })

  it('reads back what Platform recorded when the batch answer times out', async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(timeout)
      .mockResolvedValueOnce(
        Response.json({
          preview_token: 'fresh',
          expires_at: '2026-11-24T23:00:00Z',
          items: [
            previewItem(ids[0], 'already_applied', 'this exact operator decision is already recorded'),
            previewItem(ids[1], 'ready', 'will set submission status to evaluating'),
            previewItem(ids[2], 'conflict', 'submission status changed'),
          ],
          ready_count: 1,
          already_applied_count: 1,
          blocked_count: 1,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)
    const timeoutSpy = vi.spyOn(AbortSignal, 'timeout')

    const result = await executeScreeningQuarantineBatch(input, 'operator@example.com')

    // The batch gets more than the generic 20 s deadline.
    expect(timeoutSpy).toHaveBeenNthCalledWith(1, 60_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [resolveUrl] = fetchMock.mock.calls[0] as [string, RequestInit]
    const [previewUrl, previewInit] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(resolveUrl).toContain('/screening-quarantines/batch-resolve')
    expect(previewUrl).toContain('/screening-quarantines/batch-preview')
    // The read-back is the read-only preview of exactly the same decisions.
    const resolveBody = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body))
    expect(JSON.parse(String(previewInit.body))).toEqual({ decisions: resolveBody.decisions })

    expect(result.reconciled_after).toBe('timeout')
    expect(result.items.map((item) => [item.quarantine_id, item.status])).toEqual([
      [ids[0], 'already_applied'],
      [ids[1], 'unconfirmed'],
      // A conflict after a lost answer may be a release that has already moved
      // the agent on, so it is not reported as a failure either.
      [ids[2], 'unconfirmed'],
    ])
    expect(result).toMatchObject({
      applied_count: 0,
      already_applied_count: 1,
      failed_count: 0,
      unconfirmed_count: 2,
    })
    expect(result.items[0].agent_status).toBe('evaluating')
    expect(result.items[1].message).toContain('Preview again')
    expect(result.items[2].message).toContain('submission status changed')
  })

  it('fails a decision Platform cannot find and reports no ruling for unconfirmed rows', async () => {
    const ruling = {
      agent_status: 'rejected',
      artifact_sha256: 'ab'.repeat(32),
      ath_review_id: null,
      ath_action_id: null,
      ath_resolved_at: null,
    }
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(timeout)
      .mockResolvedValueOnce(
        Response.json({
          preview_token: 'fresh',
          expires_at: '2026-11-24T23:00:00Z',
          items: [
            previewItem(ids[0], 'not_found', 'quarantine not found'),
            {
              ...previewItem(ids[1], 'ready', 'will close the orphaned quarantine'),
              terminal_reconciliation: true,
              terminal_ruling: ruling,
            },
          ],
          ready_count: 1,
          already_applied_count: 0,
          blocked_count: 1,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const result = await executeScreeningQuarantineBatch(
      { ...input, decisions: decisions.slice(0, 2) },
      'operator@example.com',
    )

    expect(result).toMatchObject({ failed_count: 1, unconfirmed_count: 1, already_applied_count: 0 })
    expect(result.items[0]).toMatchObject({ status: 'failed', message: 'Not applied: quarantine not found' })
    // The preview's ruling belongs to the proposed decision, not an outcome.
    expect(result.items[1]).toMatchObject({
      status: 'unconfirmed',
      terminal_reconciliation: false,
      terminal_ruling: null,
    })
  })

  it('reads back after a gateway timeout answer', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response('upstream request timeout', { status: 504 }))
      .mockResolvedValueOnce(
        Response.json({
          preview_token: 'fresh',
          expires_at: '2026-11-24T23:00:00Z',
          items: [previewItem(ids[0], 'already_applied', 'this exact operator decision is already recorded')],
          ready_count: 0,
          already_applied_count: 1,
          blocked_count: 0,
        }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const result = await executeScreeningQuarantineBatch(
      { ...input, decisions: decisions.slice(0, 1) },
      'operator@example.com',
    )

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(result).toMatchObject({ reconciled_after: 'gateway', already_applied_count: 1 })
    expect(result.items[0].message).toContain('timed out')
  })

  it('keeps the uncertainty when the read-back fails too', async () => {
    const fetchMock = vi.fn().mockImplementation(timeout)
    vi.stubGlobal('fetch', fetchMock)

    const error = await executeScreeningQuarantineBatch(input, 'operator@example.com').catch(
      (cause: unknown) => cause,
    )

    expect(error).toBeInstanceOf(PlatformAdminError)
    expect((error as PlatformAdminError).failure).toBe('timeout')
    expect((error as PlatformAdminError).message).toContain('may have been applied')
    expect((error as PlatformAdminError).message).toContain('Preview the batch again')
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('reports a definite refusal as is, without a read-back', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        Response.json({ detail: 'terminal ruling changed after preview; preview again' }, { status: 409 }),
      )
    vi.stubGlobal('fetch', fetchMock)

    await expect(executeScreeningQuarantineBatch(input, 'operator@example.com')).rejects.toThrow(
      /preview again/,
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('returns Platform answer unchanged when it arrives', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        items: ids.map((quarantineId) => ({
          quarantine_id: quarantineId,
          status: 'applied',
          agent_status: 'evaluating',
          terminal_reconciliation: false,
          terminal_ruling: null,
          message: 'decision applied and audit event recorded',
        })),
        applied_count: 3,
        already_applied_count: 0,
        failed_count: 0,
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await executeScreeningQuarantineBatch(input, 'operator@example.com')

    expect(result).toMatchObject({
      applied_count: 3,
      unconfirmed_count: 0,
      reconciled_after: null,
    })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})
