import { createServerFn } from '@tanstack/react-start'
import { setResponseHeader } from '@tanstack/react-start/server'
import { recordTreasurySettingsInputSchema } from '../lib/treasury.schemas'
import { fetchTreasuryLedgerReadiness, fetchTreasuryRuntime, fetchTreasurySettings, recordTreasurySettings } from './admin.service'
import { authMiddleware, sameOriginMiddleware, writeAuthMiddleware } from './auth.functions'

export const getTreasurySettings = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchTreasurySettings()
  })

export const getTreasuryRuntime = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchTreasuryRuntime()
  })

export const getTreasuryLedgerReadiness = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .handler(() => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchTreasuryLedgerReadiness()
  })

export const saveTreasurySettings = createServerFn({ method: 'POST' })
  .middleware([writeAuthMiddleware, sameOriginMiddleware])
  .validator(recordTreasurySettingsInputSchema)
  .handler(({ context, data }) => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return recordTreasurySettings(data, context.session.email)
  })
