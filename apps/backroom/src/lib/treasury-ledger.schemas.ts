import { z } from 'zod'
import type { components } from '../generated/platform-api'

// Known public fields only: serializable read evidence, never spending authority.
// Platform remains responsible for policy digest and finalized identity binding.
const address = z.string().regex(/^[1-9A-HJ-NP-Za-km-z]{47,48}$/)
const hash = z.string().regex(/^0x[0-9a-f]{64}$/)
const digest = z.string().regex(/^[0-9a-f]{64}$/)
const policy = z.object({
  version: z.literal(1),
  revision: z.number().int().min(1),
  genesis_hash: hash,
  netuid: z.literal(118),
  collector_hotkey: address,
  collector_coldkey: address,
  collector_policy_digest: digest,
  buckets: z.array(z.object({
    bucket_id: z.string().regex(/^[a-z][a-z0-9_]{1,47}$/),
    allocation_bps: z.number().int().min(0).max(1000),
    holding_coldkey: address,
  })).min(1).max(20),
}) satisfies z.ZodType<components['schemas']['TreasuryEmissionPolicy']>

const identity = z.object({
  genesis_hash: hash,
  netuid: z.literal(118),
  finalized_block: z.number().int().nonnegative(),
  finalized_block_hash: hash,
  uid: z.number().int().min(0).max(65535),
  hotkey: address,
  owner_coldkey: address,
  uid_hotkey: address,
  subnet_owner_coldkey: address,
}) satisfies z.ZodType<components['schemas']['TreasuryCollectorIdentity']>

const pin = z.object({
  version: z.literal(1),
  mode: z.literal('shadow'),
  policy,
  policy_digest: digest,
  identity,
}) satisfies z.ZodType<components['schemas']['TreasuryLedgerPin']>

const fleetMember = z.object({
  validator_hotkey: address,
  protocol_version: z.number().int().min(30),
  treasury_pin_version: z.literal(2),
  treasury_dispatch_version: z.literal(2),
  approved_policy_digest: digest,
  collector_policy_digest: digest,
})

const enforcingPin = z.object({
  version: z.literal(2),
  mode: z.literal('enforce'),
  epoch_index: z.number().int().nonnegative(),
  first_block: z.number().int().nonnegative(),
  pinned_block: z.number().int().nonnegative(),
  pinned_block_hash: hash,
  policy,
  policy_digest: digest,
  identity,
  approval: z.object({
    policy,
    signature: z.string().regex(/^0x[0-9a-f]{128}$/),
  }),
  fleet: z.array(fleetMember).min(1).max(4096),
}) satisfies z.ZodType<components['schemas']['EnforcingTreasuryPin']>

export const treasuryLedgerReadinessSchema = z.object({
  configured_proposal: policy.nullable(),
  proposal_approval_status: z.enum(['not_configured', 'verified', 'invalid']).default('not_configured'),
  proposal_approved_policy_digest: digest.nullable().default(null),
  observer_status: z.enum(['disabled', 'not_observed', 'observing', 'observed', 'unavailable']),
  observer_scope: z.literal('this_platform_process'),
  latest_stored_epoch_index: z.number().int().nonnegative().nullable(),
  latest_stored_ledger_digest: digest.nullable(),
  stored_shadow_pin: pin.nullable(),
  stored_enforcing_pin: enforcingPin.nullable().default(null),
  enforcement_configured: z.boolean().default(false),
  fleet_gate: z.enum(['not_checked', 'ready', 'not_ready']).default('not_checked'),
  blocking_reasons: z.array(z.enum([
    'producer_disabled', 'no_epoch_pin', 'stored_pin_invalid', 'proposal_pin_mismatch',
    'shadow_only', 'offline_policy_unverified', 'weight_adapter_not_active',
    'fleet_gate_unimplemented', 'current_epoch_not_checked',
    'fleet_not_ready', 'enforcing_pin_unverified',
  ])).max(20),
  offline_policy_verified: z.literal(false),
  offline_epoch_verified: z.boolean().default(false),
  weight_effect: z.literal('none'),
  can_enforce_weights: z.boolean(),
}).superRefine((value, context) => {
  if ((value.proposal_approval_status === 'verified') !== (value.proposal_approved_policy_digest !== null)
    || (value.proposal_approval_status === 'verified' && value.configured_proposal === null)) {
    context.addIssue({ code: 'custom', message: 'Proposal approval must bind its exact digest and policy' })
  }
  if (value.can_enforce_weights && (!value.enforcement_configured
    || value.stored_enforcing_pin === null || value.fleet_gate !== 'ready'
    || !value.offline_epoch_verified || value.blocking_reasons.length !== 0
    || value.proposal_approval_status !== 'verified')) {
    context.addIssue({ code: 'custom', message: 'Weight readiness requires a verified enforcing epoch and complete fleet' })
  }
}) satisfies z.ZodType<components['schemas']['TreasuryLedgerReadiness']>
