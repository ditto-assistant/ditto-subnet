import '@tanstack/react-start/server-only'
import { uploadPaymentProofInputSchema, uploadPaymentProofSchema } from '../lib/upload-payment-proof.schemas'
import { platformAdminRequest } from './ditto.server'

export async function fetchUploadPaymentProof(raw: unknown) {
  const input = uploadPaymentProofInputSchema.parse(raw)
  return uploadPaymentProofSchema.parse(await platformAdminRequest(
    `/api/v1/admin/upload-payments/${input.blockHash.toLowerCase()}/${input.extrinsicIndex}`,
  ))
}
