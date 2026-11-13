import {
  edgeLaneLedgerInputSchema,
  edgeLaneLedgerSchema,
  edgeLaneListInputSchema,
  edgeLaneNodesSchema,
  edgeLaneOverviewSchema,
  edgeLaneSettingsControlSchema,
} from '../lib/edge-lane.schemas'
import { platformAdminRequest } from './ditto.server'

// Ditto Edge shadow lane (SN-481). Read-only: Backroom changes none of it.
const SETTINGS_PATH = '/api/v1/admin/edge-lane-settings'
const BASE = '/api/v1/admin/edge'

export async function fetchEdgeLaneSettings() {
  return edgeLaneSettingsControlSchema.parse(await platformAdminRequest(SETTINGS_PATH))
}

export async function fetchEdgeLaneOverview() {
  return edgeLaneOverviewSchema.parse(await platformAdminRequest(`${BASE}/overview`))
}

export async function listEdgeLaneNodes(rawInput: unknown) {
  const { limit } = edgeLaneListInputSchema.parse(rawInput)
  const params = new URLSearchParams({ limit: String(limit) })
  return edgeLaneNodesSchema.parse(await platformAdminRequest(`${BASE}/nodes?${params}`))
}

// Platform scores any window that has closed since the last poll before it
// answers: the same insert-once scoring every node poll runs, shadow-only.
export async function fetchEdgeLaneLedger(rawInput: unknown) {
  const input = edgeLaneLedgerInputSchema.parse(rawInput)
  const params = new URLSearchParams({ limit: String(input.limit) })
  if (input.windowIndex !== undefined) params.set('window_index', String(input.windowIndex))
  if (input.nodeId !== undefined) params.set('node_id', input.nodeId)
  return edgeLaneLedgerSchema.parse(await platformAdminRequest(`${BASE}/ledger?${params}`))
}
