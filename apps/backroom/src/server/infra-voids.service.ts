import '@tanstack/react-start/server-only'
import {
  getProviderOutagesInputSchema,
  infraVoidListSchema,
  listInfraVoidsInputSchema,
  providerOutagesSchema,
  voidInfraScoresInputSchema,
  voidInfraScoresResponseSchema,
} from '../lib/infra-voids.schemas'
import { platformAdminRequest } from './ditto.server'

export async function voidInfraScores(raw: unknown, actor: string) {
  const input = voidInfraScoresInputSchema.parse(raw)
  if (!input.dryRun && input.confirmation === undefined) {
    throw new Error('confirmation "VOID INFRA SCORES" is required when dryRun is false')
  }
  const payload = await platformAdminRequest('/api/v1/admin/confirmation-score-voids', {
    method: 'POST',
    actor,
    body: {
      items: input.items.map((item) => ({
        agent_id: item.agentId,
        validator_hotkey: item.validatorHotkey,
        seed: item.seed,
        expected_run_id: item.expectedRunId,
        expected_composite: item.expectedComposite,
      })),
      reason: input.reason,
      dry_run: input.dryRun,
      confirmation: input.confirmation ?? null,
    },
  })
  return voidInfraScoresResponseSchema.parse(payload)
}

export async function fetchInfraVoids(raw: unknown) {
  const input = listInfraVoidsInputSchema.parse(raw)
  const query = new URLSearchParams()
  if (input.agentId) query.set('agent_id', input.agentId)
  if (input.source) query.set('source', input.source)
  if (input.since) query.set('since', input.since)
  query.set('limit', String(input.limit))
  query.set('offset', String(input.offset))
  return infraVoidListSchema.parse(
    await platformAdminRequest(`/api/v1/admin/confirmation-score-voids?${query}`),
  )
}

export async function fetchProviderOutages(raw: unknown) {
  const input = getProviderOutagesInputSchema.parse(raw)
  const query = new URLSearchParams()
  if (input.withFindings) query.set('with_findings', 'true')
  query.set('limit', String(input.limit))
  query.set('offset', String(input.offset))
  return providerOutagesSchema.parse(
    await platformAdminRequest(`/api/v1/admin/provider-outages?${query}`),
  )
}
