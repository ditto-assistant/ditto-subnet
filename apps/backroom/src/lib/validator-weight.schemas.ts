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
  tempo: counter.min(1).max(50400),
  blocks_since_last_step: counter,
  // The stateful boundary ending this epoch, from the same should_run_epoch
  // simulation drand 2.0 and the Pylon image use.
  next_epoch_block: counter,
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
    commit_block_timestamp: counter.nullable(),
    // Which block the committer's drand round targets (about one block of
    // noise). Offset ~+3 from the boundary ending its epoch is the stateful
    // schedule; a large negative offset is the legacy same-epoch reveal lane.
    implied_reveal_block: counter.nullable(),
    implied_reveal_offset_blocks: z.number().int().nullable(),
  })).max(2560),
  historical_clipping_verified: z.literal(false),
  weights_submitted: z.literal(false),
})
