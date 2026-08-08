import { createServerFn } from '@tanstack/react-start'
import { getRequest } from '@tanstack/react-start/server'
import { z } from 'zod'
import { authMiddleware } from './auth.functions'
import { getMcpConsentDetails } from './mcp-oauth.server'

export const readMcpConsentRequest = createServerFn({ method: 'GET' })
  .middleware([authMiddleware])
  .validator(z.object({ requestToken: z.string().min(32).max(32_768) }))
  .handler(async ({ data }) =>
    getMcpConsentDetails(data.requestToken, new URL(getRequest().url).origin),
  )
