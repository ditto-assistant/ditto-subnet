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

// SN-485: the readmission lane's record of a proof. admitted: it paid this
// readmission (state funded_readmission). credited: refused after payment and
// banked as a 30-day upload credit (state reusable_credit until spent).
const readmissionRecordSchema = z.object({
  agent_id: z.string().uuid(),
  bench_version: z.number().int(),
  rollout_id: z.string().uuid().nullable(),
  outcome: z.enum(['admitted', 'credited']),
  funding: z.enum(['fresh', 'credit']),
  refusal_reason: z.string().nullable(),
  credit_audit: z.record(z.string(), z.unknown()).nullable(),
  recorded_at: z.string(),
})

export const uploadPaymentProofSchema = z.object({
  block_hash: z.string(),
  extrinsic_index: z.number().int().nonnegative(),
  state: z.enum([
    'unrecorded',
    'funded_agent',
    'reusable_credit',
    'funded_generator',
    'funded_readmission',
  ]),
  miner_hotkey: z.string().nullable(),
  // Payment-time signer of the transfer, not on-chain ownership today.
  miner_coldkey: z.string().nullable(),
  amount_rao: z.number().int().nullable(),
  dest_address: z.string().nullable(),
  block_timestamp: z.string().nullable(),
  recorded_at: z.string().nullable(),
  agent: fundedAgentSchema.nullable(),
  generator_submission_id: z.string().uuid().nullable(),
  // A Platform that predates SN-485 omits it.
  readmission: readmissionRecordSchema.nullable().default(null),
  // Credits only: block time plus the recovery window (24 hours, or 30 days
  // for a credit a refused readmission banked).
  recovery_expires_at: z.string().nullable(),
  recovery_expired: z.boolean().nullable(),
  generated_at: z.string(),
})
