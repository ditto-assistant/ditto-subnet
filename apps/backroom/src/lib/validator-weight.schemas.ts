import { z } from 'zod'

const counter = z.number().int().nonnegative()
const u16 = counter.max(65535)
const hotkey = z.string().min(1).max(64)

export const validatorWeightDiagnosticsInputSchema = z.object({
  validatorUid: u16.optional(),
})

// Strip every unknown field, including future raw commitment payloads.
export const validatorWeightDiagnosticsSchema = z.object({
  netuid: u16,
  block: counter,
  block_hash: z.string().regex(/^0x[0-9a-fA-F]{64}$/),
  last_epoch_block: counter,
  pending_epoch_at: counter,
  subnet_epoch_index: counter,
  epoch: z.object({
    tempo_blocks: counter.positive(),
    block_seconds: z.number().positive(),
    epoch_seconds: z.number().positive(),
    last_epoch_block: counter,
    next_epoch_block: counter,
    blocks_since_last_epoch: counter,
    blocks_until_next_epoch: counter,
    next_epoch_at: z.string(),
    commit_reveal_enabled: z.boolean().nullable(),
    reveal_period_epochs: counter.nullable(),
    weights_rate_limit_blocks: counter.nullable(),
  }).nullable(),
  validators: z.array(z.object({
    validator_uid: u16,
    validator_hotkey: hotkey,
    validator_trust_u16: u16,
    validator_trust: z.number().min(0).max(1),
    last_update_block: counter,
    weights: z.array(z.object({ uid: u16, hotkey, value: u16.positive() })).max(65536),
  })).max(256),
  consensus: z.array(z.object({ uid: u16, value: u16 })).max(65536),
  pending_commits: z.array(z.object({
    validator_hotkey: hotkey,
    commit_epoch: counter,
    commit_block: counter,
    reveal_round: counter,
  })).max(2560),
  historical_clipping_verified: z.literal(false),
  weights_submitted: z.literal(false),
})
