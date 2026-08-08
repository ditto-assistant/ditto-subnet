import '@tanstack/react-start/server-only'

import type { AuthRequest, ClientInfo, OAuthHelpers } from '@cloudflare/workers-oauth-provider'
import { z } from 'zod'
import {
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  type BackroomEnv,
  type McpGrantProps,
} from './mcp.server'
import { accessLevelForEmail } from '../lib/auth.policy'
import { constantTimeEqual, randomToken, sealToken, unsealToken } from './crypto.server'
import { readSessionFromRequest } from './session.server'

const PENDING_AUTH_MAX_AGE_MS = 10 * 60 * 1_000
const SUPPORTED_SCOPES = new Set([
  BACKROOM_READ_SCOPE,
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_WRITE_SCOPE,
])

type PendingAuthorization = {
  version: 1
  kind: 'backroom-mcp-authorization'
  request: AuthRequest
  client: Pick<ClientInfo, 'clientId' | 'clientName' | 'clientUri' | 'logoUri'>
  csrf: string
  origin: string
  createdAt: number
}

export type McpConsentDetails = {
  clientName: string
  clientUri: string
  logoUri: string
  requestedScopes: Array<string>
  canRequestArtifact: boolean
  canRequestWrite: boolean
  csrf: string
}

function noStoreJson(value: unknown, status = 200) {
  return Response.json(value, {
    status,
    headers: {
      'Cache-Control': 'no-store',
      Pragma: 'no-cache',
    },
  })
}

function normalizeScopes(scopes: Array<string>) {
  const requested = scopes.length > 0 ? scopes : [BACKROOM_READ_SCOPE]
  if (requested.some((scope) => !SUPPORTED_SCOPES.has(scope))) {
    throw new Error('The client requested an unsupported Backroom scope')
  }
  if (
    (requested.includes(BACKROOM_ARTIFACT_SCOPE) || requested.includes(BACKROOM_WRITE_SCOPE)) &&
    !requested.includes(BACKROOM_READ_SCOPE)
  ) {
    throw new Error('Privileged Backroom scopes must be requested with backroom:read')
  }
  return [...new Set(requested)]
}

async function decodePendingAuthorization(
  token: string,
  secret = process.env.SESSION_SECRET,
  expectedOrigin?: string,
) {
  const pending = await unsealToken<PendingAuthorization>(token, secret)
  if (
    !pending ||
    pending.version !== 1 ||
    pending.kind !== 'backroom-mcp-authorization' ||
    !pending.request.clientId ||
    !pending.request.redirectUri ||
    !pending.csrf ||
    !pending.origin ||
    (expectedOrigin && pending.origin !== expectedOrigin) ||
    Date.now() - pending.createdAt > PENDING_AUTH_MAX_AGE_MS
  ) {
    throw new Error('This MCP authorization request expired. Start connecting again.')
  }
  return pending
}

export async function beginMcpAuthorization(request: Request, oauth: OAuthHelpers, secret: string) {
  const oauthRequest = await oauth.parseAuthRequest(request)
  oauthRequest.scope = normalizeScopes(oauthRequest.scope)

  const expectedResource = `${new URL(request.url).origin}/mcp`
  const resources = Array.isArray(oauthRequest.resource)
    ? oauthRequest.resource
    : oauthRequest.resource
      ? [oauthRequest.resource]
      : []
  if (resources.length !== 1 || resources[0] !== expectedResource) {
    throw new Error(`The OAuth resource must be ${expectedResource}`)
  }

  const client = await oauth.lookupClient(oauthRequest.clientId)
  if (!client) throw new Error('The OAuth client is not registered')

  const pending: PendingAuthorization = {
    version: 1,
    kind: 'backroom-mcp-authorization',
    request: oauthRequest,
    client: {
      clientId: client.clientId,
      clientName: client.clientName,
      clientUri: client.clientUri,
      logoUri: client.logoUri,
    },
    csrf: randomToken(),
    origin: new URL(request.url).origin,
    createdAt: Date.now(),
  }
  const token = await sealToken(pending, secret)
  const destination = new URL('/oauth/consent', request.url)
  destination.searchParams.set('request', token)
  return Response.redirect(destination, 302)
}

export async function getMcpConsentDetails(
  token: string,
  expectedOrigin: string,
  secret = process.env.SESSION_SECRET,
): Promise<McpConsentDetails> {
  const pending = await decodePendingAuthorization(token, secret, expectedOrigin)
  const scopes = normalizeScopes(pending.request.scope)
  return {
    clientName: pending.client.clientName?.trim() || 'An MCP client',
    clientUri: pending.client.clientUri ?? '',
    logoUri: pending.client.logoUri ?? '',
    requestedScopes: scopes,
    canRequestArtifact: scopes.includes(BACKROOM_ARTIFACT_SCOPE),
    canRequestWrite: scopes.includes(BACKROOM_WRITE_SCOPE),
    csrf: pending.csrf,
  }
}

function deniedRedirect(pending: PendingAuthorization) {
  const redirect = new URL(pending.request.redirectUri)
  redirect.searchParams.set('error', 'access_denied')
  redirect.searchParams.set('error_description', 'The user declined Backroom access')
  if (pending.request.state) {
    redirect.searchParams.set('state', pending.request.state)
  }
  return redirect.toString()
}

export async function completeMcpAuthorization(
  request: Request,
  env: BackroomEnv & { OAUTH_PROVIDER: OAuthHelpers },
) {
  const origin = new URL(request.url).origin
  if (request.headers.get('origin') !== origin) {
    return noStoreJson({ error: 'Origin check failed' }, 403)
  }
  if (!request.headers.get('content-type')?.includes('application/json')) {
    return noStoreJson({ error: 'Content-Type must be application/json' }, 415)
  }

  const input = z
    .object({
      requestToken: z.string().min(32).max(32_768),
      csrf: z.string().min(16).max(256),
      decision: z.enum(['allow', 'deny']),
      accessLevel: z.enum(['read', 'artifact', 'write', 'full']).default('read'),
    })
    .parse(await request.json())
  const pending = await decodePendingAuthorization(input.requestToken, env.SESSION_SECRET, origin)
  if (!constantTimeEqual(input.csrf, pending.csrf)) {
    return noStoreJson({ error: 'Invalid consent token' }, 403)
  }
  if (input.decision === 'deny') {
    return noStoreJson({ redirectTo: deniedRedirect(pending) })
  }

  let session = await readSessionFromRequest(request, env.SESSION_SECRET)
  if (!session) return noStoreJson({ error: 'Your Backroom session expired' }, 401)
  // Re-derive the access level from the binding rather than trusting the level
  // sealed into the cookie. This is the same revocation model the console
  // documents: removing an address from BACKROOM_ADMIN_EMAILS drops write
  // access on the next request, and here that means the next authorization
  // cannot mint a privileged grant from a session issued while it still had one.
  let accessLevel: 'read' | 'write'
  try {
    accessLevel = accessLevelForEmail(session.email, env.BACKROOM_ADMIN_EMAILS)
  } catch {
    return noStoreJson({ error: 'This account is not authorized to enter Backroom' }, 403)
  }
  session = { ...session, accessLevel }

  // The consenting staff member picks the level; the only hard limit is the
  // account's own Backroom access, so a read-only account can never elevate a
  // connection to source-download or write access.
  const canGrantPrivileged = session.accessLevel === 'write'
  const grantArtifact =
    (input.accessLevel === 'artifact' || input.accessLevel === 'full') && canGrantPrivileged
  const grantWrite =
    (input.accessLevel === 'write' || input.accessLevel === 'full') && canGrantPrivileged
  const scopes = [
    BACKROOM_READ_SCOPE,
    ...(grantArtifact ? [BACKROOM_ARTIFACT_SCOPE] : []),
    ...(grantWrite ? [BACKROOM_WRITE_SCOPE] : []),
  ]

  const props: McpGrantProps = {
    session,
    scopes,
    clientName: pending.client.clientName?.trim() || 'MCP client',
  }
  const { redirectTo } = await env.OAUTH_PROVIDER.completeAuthorization({
    request: pending.request,
    userId: session.uid,
    metadata: {
      clientName: props.clientName,
      email: session.email,
      accessLevel: grantWrite
        ? grantArtifact
          ? 'full'
          : 'read-write'
        : grantArtifact
          ? 'read-artifacts'
          : 'read-only',
      authorizedAt: new Date().toISOString(),
    },
    scope: scopes,
    props,
  })
  return noStoreJson({ redirectTo })
}
