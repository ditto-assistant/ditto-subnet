import '@tanstack/react-start/server-only'
import { z } from 'zod'
import {
  officialControlApprovalSchema, officialControlApprovalViewSchema, officialControlArtifactSchema,
  officialControlImageSchema, officialControlLookupSchema,
  officialControlsInputSchema, officialControlStageSchema,
  officialControlUploadSchema, officialControlVerifySchema,
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
    `${base}/${input.controlId}/artifact`, { actor },
  ))
}

export async function stageOfficialV13ControlImage(raw: unknown, actor: string) {
  const input = officialControlStageSchema.parse(raw)
  return officialControlUploadSchema.parse(await platformAdminRequest(`${base}/images`, {
    method: 'POST', actor, operatorProof: true,
    body: { request_id: input.requestId, fixture_id: input.fixtureId,
      image_sha256: input.imageSha256, image_id: input.imageId, size_bytes: input.sizeBytes },
  }))
}

export async function verifyOfficialV13ControlImage(raw: unknown, actor: string) {
  const input = officialControlVerifySchema.parse(raw)
  return officialControlImageSchema.parse(await platformAdminRequest(`${base}/${input.controlId}/verify`, {
    method: 'POST', actor, operatorProof: true, timeoutMs: 180_000,
    body: { image_sha256: input.imageSha256, image_id: input.imageId },
  }))
}

export async function recordOfficialV13ControlApproval(raw: unknown, actor: string) {
  const input = officialControlApprovalSchema.parse(raw)
  return officialControlApprovalViewSchema.parse(await platformAdminRequest(`${base}/${input.controlId}/approvals`, {
    method: 'POST', actor, operatorProof: true,
    body: { image_sha256: input.imageSha256, image_id: input.imageId,
      profile_sha256: input.profileSha256, review_evidence_sha256: input.reviewEvidenceSha256,
      reason: input.reason },
  }))
}
