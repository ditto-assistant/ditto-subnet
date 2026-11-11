import { z } from 'zod'

// One finalized upload-fee transfer, as the miner CLI prints it. The block
// number is not needed: Platform keys its payment records by hash and index.
export const uploadPaymentProofInputSchema = z.object({
  blockHash: z.string().trim().regex(/^0x[0-9a-fA-F]{64}$/),
  extrinsicIndex: z.number().int().min(0).max(2_147_483_647),
})

const fundedAgentSchema = z.object({
  agent_id: z.string().uuid(),
  agent_name: z.string(),
  agent_version: z.number().int().nullable(),
  agent_status: z.string(),
  artifact_sha256: z.string(),
  submitted_at: z.string(),
})

export const uploadPaymentProofSchema = z.object({
  block_hash: z.string(),
  extrinsic_index: z.number().int().nonnegative(),
  state: z.enum(['unrecorded', 'funded_agent', 'reusable_credit', 'funded_generator']),
  miner_hotkey: z.string().nullable(),
  // Payment-time signer of the transfer, not on-chain ownership today.
  miner_coldkey: z.string().nullable(),
  amount_rao: z.number().int().nullable(),
  dest_address: z.string().nullable(),
  block_timestamp: z.string().nullable(),
  recorded_at: z.string().nullable(),
  agent: fundedAgentSchema.nullable(),
  generator_submission_id: z.string().uuid().nullable(),
  // Credits only: block time plus the 24-hour recovery window.
  recovery_expires_at: z.string().nullable(),
  recovery_expired: z.boolean().nullable(),
  generated_at: z.string(),
})
