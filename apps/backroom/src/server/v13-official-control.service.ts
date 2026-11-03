import '@tanstack/react-start/server-only'
import { z } from 'zod'
import {
  officialControlApprovalViewSchema, officialControlArtifactSchema,
  officialControlImageSchema, officialControlLookupSchema,
  officialControlsInputSchema,
} from '../lib/v13-official-control.schemas'
import { platformAdminRequest } from './ditto.server'

const base = '/api/v1/admin/v13-official-controls'

export async function fetchOfficialV13Controls(raw: unknown) {
  const input = officialControlsInputSchema.parse(raw)
  return z.array(officialControlImageSchema).parse(await platformAdminRequest(
    `${base}?limit=${input.limit}&offset=${input.offset}`,
  ))
}

export async function fetchOfficialV13ControlApprovals(raw: unknown) {
  const input = officialControlLookupSchema.parse(raw)
  return z.array(officialControlApprovalViewSchema).parse(await platformAdminRequest(
    `${base}/${input.controlId}/approvals`,
  ))
}

export async function fetchOfficialV13ControlArtifact(raw: unknown, actor: string) {
  const input = officialControlLookupSchema.parse(raw)
  return officialControlArtifactSchema.parse(await platformAdminRequest(
    `${base}/${input.controlId}/artifact`, { actor, operatorProof: true, timeoutMs: 180_000 },
  ))
}
