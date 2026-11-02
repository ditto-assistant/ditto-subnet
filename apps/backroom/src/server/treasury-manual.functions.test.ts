import { beforeEach, expect, it, vi } from 'vitest'
import { manualTransferConfirmation } from '../lib/treasury-manual.schemas'
import { queueManualTransfer } from './treasury-manual.functions'

const { platformRequest } = vi.hoisted(() => ({ platformRequest: vi.fn() }))
vi.mock('./ditto.server', () => ({ platformAdminRequest: platformRequest }))
vi.mock('./auth.functions', () => ({ authMiddleware: {}, writeAuthMiddleware: {}, sameOriginMiddleware: {} }))
vi.mock('@tanstack/react-start/server', () => ({ setResponseHeader: vi.fn() }))
vi.mock('@tanstack/react-start', () => ({ createServerFn: () => {
  type Validator = { parse: (data: unknown) => unknown }
  type Builder = {
    middleware: () => Builder
    validator: (schema: Validator) => Builder
    handler: (handler: (input: unknown) => unknown) => (input: { data: unknown }) => Promise<unknown>
  }
  let validator: Validator
  const builder: Builder = {
    middleware: () => builder,
    validator: (schema: typeof validator) => { validator = schema; return builder },
    handler: (handler: (input: unknown) => unknown) => async (input: { data: unknown }) => handler({
      context: { session: { email: 'operator@example.com' } }, data: validator.parse(input.data),
    }),
  }
  return builder
} }))
const envelope = {
  version: 1 as const, collector_policy_digest: 'a'.repeat(64), destination: '5' + 'a'.repeat(47),
  request: { request_id: 'fa1e68a6-5213-4b25-b90a-bec349ab0a0b', after_operation: 2, source_block: 200,
    bucket_id: 'gm', amount_rao: 100000000, retained_alpha_rao: 55000000000, expires_block: 1000,
    reason: 'manual inference purchase' },
}
const payload = { envelope, confirmation_digest: 'b'.repeat(64), confirmation: manualTransferConfirmation(envelope) }
beforeEach(() => {
  vi.clearAllMocks()
  platformRequest.mockResolvedValue({ request_id: envelope.request.request_id, envelope, status: 'queued',
    actor: 'operator@example.com', created_at: '2026-10-06T00:00:00Z', updated_at: '2026-10-06T00:00:00Z', last_error: null, receipt: null })
})
it.each(['', 'TRANSFER SN118 ALPHA ONCE', 'TRANSFER 0.2 ALPHA TO GM ' + envelope.destination,
  'TRANSFER 0.1 ALPHA TO GM ' + '5' + 'b'.repeat(47)])('rejects wrong or absent confirmation before Platform: %s', async confirmation => {
  await expect(queueManualTransfer({ data: { ...payload, confirmation } })).rejects.toThrow()
  expect(platformRequest).not.toHaveBeenCalled()
})
it('rejects an omitted phrase before Platform', async () => {
  const missing = { envelope: payload.envelope, confirmation_digest: payload.confirmation_digest }
  await expect(queueManualTransfer({ data: missing as typeof payload })).rejects.toThrow()
  expect(platformRequest).not.toHaveBeenCalled()
})
it('accepts only the exact amount and address, then adapts the Platform wire literal', async () => {
  const result = await queueManualTransfer({ data: payload })
  expect(result.status).toBe('queued')
  expect(platformRequest).toHaveBeenCalledTimes(1)
  expect(platformRequest).toHaveBeenCalledWith('/api/v1/admin/treasury-manual', {
    method: 'POST', actor: 'operator@example.com',
    body: { ...payload, confirmation: 'TRANSFER SN118 ALPHA ONCE' },
  })
})
