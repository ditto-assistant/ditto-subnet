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
  ledger_schedule_probe_status: z.enum(['not_checked', 'available', 'unavailable']).default('not_checked'),
  ledger_schedule_probe_epoch: z.number().int().nonnegative().nullable().default(null),
  ledger_schedule_probe_block: z.number().int().nonnegative().nullable().default(null),
  ledger_schedule_matches_stored_pin: z.boolean().nullable().default(null),
  ledger_schedule_failure_kind: z.enum(['timeout', 'connection', 'reader_unavailable', 'unavailable']).nullable().default(null),
  validation_failure_stage: z.enum(['fleet_binding', 'identity', 'setter_roster', 'authority']).nullable().optional(),
  validation_failure_step: z.enum([
    'connection', 'connection_close', 'finalized_head', 'finalized_height',
    'canonical_hash', 'genesis_hash', 'epoch_storage', 'collector_storage',
    'uid_binding', 'permit_vector', 'setter_binding',
  ]).nullable().optional(),
  validation_failure_kind: z.enum(['timeout', 'connection', 'invalid_evidence', 'reader_unavailable', 'unavailable']).nullable().optional(),
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


const managedRoster = z.array(address).min(1).max(128).refine(keys => new Set(keys).size === keys.length, 'Managed roster must be distinct')
export const treasuryActivationPreflightInputSchema = z.object({
  approvalJson: z.string().min(1).max(8192),
  expectedPolicyDigest: digest,
  expectedCollectorPolicyDigest: digest,
  managedValidatorHotkeys: managedRoster.optional(),
})

export const publicTreasuryApprovalSchema = z.object({
  policy,
  signature: z.string().regex(/^0x[0-9a-f]{128}$/),
})

export const treasuryRuntimeSettingsSchema = z.object({
  version: z.literal(1), mode: z.enum(['observe', 'enforce', 'pause']),
  approval: publicTreasuryApprovalSchema, approved_policy_digest: digest,
  collector_policy_digest: digest, managed_validator_hotkeys: managedRoster, activation_epoch: z.number().int().nonnegative().nullable().default(null),
})
export const treasuryRuntimeRevisionSchema = z.object({
  revision: z.number().int().positive(), parent_revision: z.number().int().nonnegative(),
  settings: treasuryRuntimeSettingsSchema, checksum: digest, actor: z.string(),
  reason: z.string(), created_at: z.string(),
})
export const treasuryRuntimeControlSchema = z.object({
  revision: z.number().int().nonnegative(), latest: treasuryRuntimeRevisionSchema.nullable(),
  can_enforce_weights: z.literal(false), transfers_enabled: z.literal(false),
})
export const recordTreasuryRuntimeInputSchema = {
  expectedRevision: z.number().int().nonnegative(),
  mode: z.enum(['observe', 'enforce', 'pause']),
  approvalJson: z.string().min(1).max(8192),
  expectedPolicyDigest: digest, expectedCollectorPolicyDigest: digest,
  managedValidatorHotkeys: managedRoster,
  activationEpoch: z.number().int().nonnegative().nullable(),
  reason: z.string().trim().min(8), confirmation: z.string().min(1).max(100),
}

export const treasuryActivationPreflightSchema = z.object({
  checked_at: z.string().datetime({ offset: true }),
  proposed_policy_digest: digest,
  proposed_collector_policy_digest: digest,
  proposal_signature_verified: z.literal(true),
  configured_policy_matches: z.boolean(),
  configured_collector_matches: z.boolean(),
  chain_status: z.enum(['verified', 'unavailable']),
  chain_failure_stage: z.enum(['identity', 'setter_roster']).nullable().default(null),
  chain_failure_step: z.enum(['connection', 'connection_close', 'finalized_head', 'finalized_height', 'canonical_hash',
    'genesis_hash', 'epoch_storage', 'collector_storage', 'uid_binding', 'permit_vector',
    'setter_binding']).nullable().default(null),
  chain_failure_kind: z.enum(['timeout', 'connection', 'invalid_evidence', 'reader_unavailable', 'unavailable']).nullable().default(null),
  observation: z.object({
    identity,
    epoch_index: z.number().int().nonnegative(),
    first_block: z.number().int().nonnegative(),
    finalized_block: z.number().int().nonnegative(),
    finalized_block_hash: hash,
  }).nullable(),
  required_setter_count: z.number().int().min(1).max(4096).nullable(),
  gate_scope: z.literal('managed_validators').default('managed_validators'),
  managed_validator_hotkeys: z.array(address).max(128).default([]),
  chain_permitted_setter_count: z.number().int().min(1).max(4096).nullable().default(null),
  setters: z.array(z.object({
    validator_hotkey: address,
    required_by_chain: z.boolean(),
    seen_at: z.string().datetime({ offset: true }).nullable(),
    protocol_version: z.number().int().nullable(),
    capability: fleetMember.omit({ validator_hotkey: true, protocol_version: true }).nullable(),
    status: z.enum(['ready', 'missing_heartbeat', 'inventory_not_checked', 'heartbeat_outside_window',
      'invalid_heartbeat', 'missing_guard', 'unsupported_protocol', 'policy_mismatch']),
  })).max(512),
  truncated: z.boolean(),
  fleet_ready_for_proposed_policy: z.boolean(),
  blocking_reasons: z.array(z.enum(['chain_unavailable', 'inventory_truncated', 'setter_proof_missing', 'managed_roster_missing', 'managed_setter_not_permitted'])).max(5),
  weight_effect: z.literal('none'),
  can_enforce_weights: z.literal(false),
  copy_behavior_verified: z.literal(false),
}).superRefine((value, context) => {
  if (value.fleet_ready_for_proposed_policy && (value.chain_status !== 'verified'
    || value.observation === null || value.required_setter_count === null
    || value.managed_validator_hotkeys.length !== value.required_setter_count
    || new Set(value.managed_validator_hotkeys).size !== value.managed_validator_hotkeys.length
    || value.setters.length !== value.managed_validator_hotkeys.length
    || value.setters.some(row => !value.managed_validator_hotkeys.includes(row.validator_hotkey))
    || new Set(value.setters.map(row => row.validator_hotkey)).size !== value.setters.length
    || value.truncated || value.blocking_reasons.length !== 0
    || value.setters.some(row => row.status !== 'ready')
    || value.setters.filter(row => row.required_by_chain).length !== value.required_setter_count)) {
    context.addIssue({ code: 'custom', message: 'Preflight fleet readiness requires complete exact-policy read evidence' })
  }
}) satisfies z.ZodType<components['schemas']['TreasuryActivationPreflight']>
