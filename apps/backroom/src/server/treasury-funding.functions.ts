import { createServerFn } from '@tanstack/react-start'
import { setResponseHeader } from '@tanstack/react-start/server'
import { authMiddleware } from './auth.functions'
import { fetchTreasuryFundingHistory } from './treasury-funding.service'

export const getTreasuryFundingHistory = createServerFn({ method: 'GET' })
  .middleware([authMiddleware]).handler(async () => {
    setResponseHeader('Cache-Control', 'no-store')
    setResponseHeader('Vary', 'Cookie, Authorization')
    return fetchTreasuryFundingHistory()
  })
