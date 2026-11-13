import { z } from 'zod'

// Ditto Edge shadow lane (SN-481): opt-in Mac nodes hold content-addressed
// public objects and answer challenges, measured into a ledger that pays
// nothing. Mirrors apps/platform/ditto/api_models/edge_lane.py. Backroom only
// reads it here; Platform stays the authority on every value and refusal.

const timestamp = z.string()

export const edgeLaneSettingsSchema = z.object({
  mode: z.enum(['off', 'shadow']),
  max_nodes: z.number().int(),
  replicas: z.number().int(),
  max_object_bytes: z.number().int(),
  max_total_object_bytes: z.number().int(),
  node_quota_cap_bytes: z.number().int(),
  lease_seconds: z.number().int(),
  poll_interval_seconds: z.number().int(),
  challenge_deadline_seconds: z.number().int(),
  window_seconds: z.number().int(),
  challenges_per_window: z.number().int(),
  retrieval_share: z.number(),
  revoked_share: z.number(),
  min_challenges: z.number().int(),
  availability_floor: z.number(),
  max_new_assignments_per_poll: z.number().int(),
  revoked_lookback_seconds: z.number().int(),
})

const edgeLaneSettingsRevisionSchema = z.object({
  revision: z.number().int(),
  parent_revision: z.number().int(),
  scope: z.string(),
  settings: edgeLaneSettingsSchema,
  reason: z.string(),
  actor: z.string(),
  created_at: timestamp,
  checksum: z.string(),
})

export const edgeLaneSettingsControlSchema = z.object({
  current: z.array(edgeLaneSettingsRevisionSchema),
  history: z.array(edgeLaneSettingsRevisionSchema),
  default: edgeLaneSettingsSchema,
  effective: z.object({
    revision: z.number().int(),
    scope: z.string(),
    settings: edgeLaneSettingsSchema,
    checksum: z.string(),
    source: z.enum(['revision', 'default']),
    max_age_seconds: z.number(),
    // Shadow only: nothing in this lane reaches weights or emissions.
    weight_eligible: z.literal(false),
  }),
})

const edgeLedgerRowSchema = z.object({
  window_index: z.number().int(),
  node_id: z.string(),
  operator: z.string(),
  scheduled: z.number().int(),
  passed: z.number().int(),
  failed: z.number().int(),
  missed: z.number().int(),
  refilled: z.number().int(),
  refused: z.number().int(),
  revocation_checks: z.number().int(),
  revocation_violations: z.number().int(),
  held_gib_hours: z.number(),
  availability: z.number(),
  eligible: z.boolean(),
  verified_units: z.number(),
  hypothetical_share: z.number(),
  reasons: z.array(z.string()),
  settings_revision: z.number().int(),
  weight_eligible: z.literal(false),
  computed_at: timestamp,
})

export const edgeLaneOverviewSchema = z.object({
  mode: z.enum(['off', 'shadow']),
  settings_revision: z.number().int(),
  weight_eligible: z.literal(false),
  payouts: z.literal('none'),
  nodes_active: z.number().int(),
  nodes_live: z.number().int(),
  nodes_suspended: z.number().int(),
  nodes_retired: z.number().int(),
  objects_active: z.number().int(),
  objects_revoked: z.number().int(),
  active_object_bytes: z.number().int(),
  open_assignments: z.number().int(),
  held_assignments: z.number().int(),
  current_window_index: z.number().int(),
  recent_windows: z.array(
    z.object({
      window_index: z.number().int(),
      settings_revision: z.number().int(),
      node_count: z.number().int(),
      eligible_nodes: z.number().int(),
      verified_units: z.number(),
      closed_at: timestamp,
    }),
  ),
})

export const edgeLaneNodesSchema = z.object({
  nodes: z.array(
    z.object({
      node_id: z.string(),
      operator: z.string(),
      hotkey_ss58: z.string().nullable(),
      client_name: z.string(),
      client_version: z.string(),
      // Self-reported by the node: shown, never scored.
      platform: z.record(z.string(), z.unknown()),
      status: z.string(),
      quota_bytes: z.number().int(),
      enrolled_at: timestamp,
      last_poll_at: timestamp.nullable(),
      held_objects: z.number().int(),
      held_bytes: z.number().int(),
      latest_window: edgeLedgerRowSchema.nullable(),
    }),
  ),
})

export const edgeLaneLedgerSchema = z.object({
  rows: z.array(edgeLedgerRowSchema),
  closed_now: z.array(z.number().int()),
})

export const edgeLaneListInputSchema = z.object({
  limit: z.number().int().min(1).max(1000).default(200),
})

export const edgeLaneLedgerInputSchema = z.object({
  windowIndex: z.number().int().min(0).optional(),
  nodeId: z.string().min(1).max(40).optional(),
  limit: z.number().int().min(1).max(1000).default(200),
})

export type EdgeLaneSettingsControl = z.infer<typeof edgeLaneSettingsControlSchema>
export type EdgeLaneOverview = z.infer<typeof edgeLaneOverviewSchema>
export type EdgeLaneNodes = z.infer<typeof edgeLaneNodesSchema>
export type EdgeLaneLedger = z.infer<typeof edgeLaneLedgerSchema>
