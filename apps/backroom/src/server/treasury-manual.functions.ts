import { createServerFn } from '@tanstack/react-start'
import { setResponseHeader } from '@tanstack/react-start/server'
import { manualControlSchema, manualPreviewInputSchema, manualPreviewSchema, manualSubmitSchema, manualTransferSchema } from '../lib/treasury-manual.schemas'
import { platformAdminRequest } from './ditto.server'
import { authMiddleware, sameOriginMiddleware, writeAuthMiddleware } from './auth.functions'

const noStore = () => {
  setResponseHeader('Cache-Control', 'no-store')
  setResponseHeader('Vary', 'Cookie, Authorization')
}
export const getManualTransfers = createServerFn({ method: 'GET' })
  .middleware([authMiddleware]).handler(async () => {
    noStore()
    return manualControlSchema.parse(await platformAdminRequest('/api/v1/admin/treasury-manual'))
  })
export const previewManualTransfer = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware]).validator(manualPreviewInputSchema)
  .handler(async ({ context, data }) => {
    noStore()
    return manualPreviewSchema.parse(await platformAdminRequest('/api/v1/admin/treasury-manual/preview', {
      method: 'POST', body: data, actor: context.session.email,
    }))
  })
export const queueManualTransfer = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware]).validator(manualSubmitSchema)
  .handler(async ({ context, data }) => {
    noStore()
    return manualTransferSchema.parse(await platformAdminRequest('/api/v1/admin/treasury-manual', {
      method: 'POST', body: data, actor: context.session.email,
    }))
  })
