import '@tanstack/react-start/server-only'
import { fundingBatchSchema, fundingSubmitSchema, fundingPreviewSchema, fundingResultSchema, fundingHistorySchema } from '../lib/treasury-funding.schemas'
import { platformAdminRequest } from './ditto.server'

export async function fetchTreasuryFundingHistory() {
  return fundingHistorySchema.parse(await platformAdminRequest('/api/v1/admin/treasury-funding'))
}
export async function previewTreasuryFunding(raw: unknown, actor: string) {
  const body = fundingBatchSchema.parse(raw)
  return fundingPreviewSchema.parse(await platformAdminRequest('/api/v1/admin/treasury-funding/preview', { method: 'POST', body, actor }))
}
export async function recordTreasuryFunding(raw: unknown, actor: string) {
  const body = fundingSubmitSchema.parse(raw)
  return fundingResultSchema.parse(await platformAdminRequest('/api/v1/admin/treasury-funding', { method: 'POST', body, actor }))
}
