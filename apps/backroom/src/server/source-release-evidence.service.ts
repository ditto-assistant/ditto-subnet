import '@tanstack/react-start/server-only'
import { sourceReleaseEvidenceInputSchema, sourceReleaseEvidenceSchema } from '../lib/source-release-evidence.schemas'
import { platformAdminRequest } from './ditto.server'

export async function fetchAgentSourceReleaseEvidence(raw: unknown) {
  const input = sourceReleaseEvidenceInputSchema.parse(raw)
  const query = input.candidateAgentId ? `?candidate_agent_id=${input.candidateAgentId}` : ''
  return sourceReleaseEvidenceSchema.parse(await platformAdminRequest(
    `/api/v1/admin/agents/${input.agentId}/source-release-evidence${query}`,
  ))
}
