import { describe, expect, it } from 'vitest'
import {
  amendQuarantineRejectReasonInputSchema,
  amendQuarantineRejectReasonResponseSchema,
} from './admin.schemas'

describe('reject reason amendment attempt IDs', () => {
  const input = {
    quarantineId: 'ee198c72-0000-4000-8000-000000000001',
    expectedAgentId: '92b9aec4-3642-4b0d-872f-9ce516cb7b7e',
    expectedArtifactSha256: '70'.repeat(32),
    expectedReason: 'Original reason',
    reason: 'Corrected reason',
    confirmation: 'AMEND PUBLIC REJECT REASON',
  }
  const response = {
    quarantine_id: input.quarantineId,
    agent_id: input.expectedAgentId,
    previous_reason: input.expectedReason,
    reason: input.reason,
    idempotent: false,
  }

  it.each([
    '5f192704-e4ff-40a4-9edc-24aac520bee2',
    '5f192704-e4ff-00a4-0edc-24aac520bee2',
  ])('accepts database attempt ID %s in requests and responses', (attemptId) => {
    expect(amendQuarantineRejectReasonInputSchema.safeParse({ ...input, expectedAttemptId: attemptId }).success).toBe(true)
    expect(amendQuarantineRejectReasonResponseSchema.safeParse({ ...response, attempt_id: attemptId }).success).toBe(true)
  })

  it('rejects malformed attempt IDs in requests and responses', () => {
    expect(amendQuarantineRejectReasonInputSchema.safeParse({ ...input, expectedAttemptId: 'invalid' }).success).toBe(false)
    expect(amendQuarantineRejectReasonResponseSchema.safeParse({ ...response, attempt_id: 'invalid' }).success).toBe(false)
  })
})
