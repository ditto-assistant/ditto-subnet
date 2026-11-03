import '@tanstack/react-start/server-only'
import { artifactFetchAuditInputSchema, artifactFetchAuditSchema } from '../lib/artifact-fetch-audit.schemas'
import { platformAdminRequest } from './ditto.server'

export async function fetchArtifactFetchAudit(raw: unknown) {
  const input = artifactFetchAuditInputSchema.parse(raw)
  const query = new URLSearchParams()
  if (input.agentId) query.set('agent_id', input.agentId)
  if (input.sha) query.set('artifact_sha256', input.sha)
  if (input.before) query.set('before', input.before)
  if (input.limit) query.set('limit', String(input.limit))
  return artifactFetchAuditSchema.parse(await platformAdminRequest(
    `/api/v1/admin/artifact-fetch-audit?${query.toString()}`,
  ))
}
