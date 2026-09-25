import '@tanstack/react-start/server-only'

import { OAuthError } from '@cloudflare/workers-oauth-provider'
import type {
  AuthRequest,
  ClientInfo,
  GrantSummary,
  OAuthHelpers,
  TokenExchangeCallbackOptions,
  TokenExchangeCallbackResult,
} from '@cloudflare/workers-oauth-provider'
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
/** Interactive MCP access-token ceiling; `server.ts` configures the same value. */
export const MAX_ACCESS_TOKEN_TTL_SECONDS = 24 * 60 * 60
/** Workers KV rejects an `expirationTtl` below 60 seconds. */
export const MIN_ACCESS_TOKEN_TTL_SECONDS = 60
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

export function accessLevelForScopes(scopes: Array<string>) {
  const artifact = scopes.includes(BACKROOM_ARTIFACT_SCOPE)
  const write = scopes.includes(BACKROOM_WRITE_SCOPE)
  return write ? (artifact ? 'full' : 'read-write') : artifact ? 'read-artifacts' : 'read-only'
}

/**
 * The scopes a consent actually grants: the intersection of what the OAuth
 * client requested, what the operator selected on the consent screen, and what
 * the operator's live Backroom level entitles. Read is always the floor. A
 * client that needs more must re-authorize with the broader scope (the MCP
 * endpoint answers insufficient_scope with exactly that step-up challenge), so
 * consent can never widen a read-only request into artifact or write access.
 */
export function grantedMcpScopes({
  requested,
  selected,
  accountLevel,
}: {
  requested: Array<string>
  selected: 'read' | 'artifact' | 'write' | 'full'
  accountLevel: 'read' | 'write'
}) {
  const privileged = accountLevel === 'write'
  const artifact =
    privileged &&
    requested.includes(BACKROOM_ARTIFACT_SCOPE) &&
    (selected === 'artifact' || selected === 'full')
  const write =
    privileged &&
    requested.includes(BACKROOM_WRITE_SCOPE) &&
    (selected === 'write' || selected === 'full')
  return [
    BACKROOM_READ_SCOPE,
    ...(artifact ? [BACKROOM_ARTIFACT_SCOPE] : []),
    ...(write ? [BACKROOM_WRITE_SCOPE] : []),
  ]
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
    accessLevel = accessLevelForEmail(
      session.email,
      env.BACKROOM_ADMIN_EMAILS,
      env.BACKROOM_BLOCKED_EMAILS,
    )
  } catch {
    return noStoreJson({ error: 'This account is not authorized to enter Backroom' }, 403)
  }
  session = { ...session, accessLevel }

  const requestedScopes = normalizeScopes(pending.request.scope)
  const scopes = grantedMcpScopes({
    requested: requestedScopes,
    selected: input.accessLevel,
    accountLevel: session.accessLevel,
  })

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
      accessLevel: accessLevelForScopes(scopes),
      requestedScopes,
      authorizedAt: new Date().toISOString(),
    },
    scope: scopes,
    props,
    // Reconnecting the same client replaces its previous grant outright, so a
    // read-only reconnect can never leave an earlier full grant (or its
    // refresh token) usable under that client id.
    revokeExistingGrants: true,
  })
  return noStoreJson({ redirectTo })
}

/**
 * Token issuance for both the authorization-code and refresh grants. The token
 * carries only scopes that the client asked for on this request AND that the
 * grant's consent recorded, and it is stamped with the exact grant and client
 * ids so `get_backroom_access` can name the grant an operator should revoke.
 * The grant's own props are never rewritten, so a narrowed token request can
 * neither widen nor permanently shrink the consented grant.
 */
export function mcpTokenExchange(
  options: TokenExchangeCallbackOptions,
  now = Date.now(),
): TokenExchangeCallbackResult {
  const props = options.props as McpGrantProps | undefined
  // A grant is only ever as live as the operator session that authorized it.
  // This deployment authenticates against Google plus BACKROOM_ADMIN_EMAILS and
  // has no refresh path, so an expired staff session ends the connection rather
  // than being silently renewed: the 7-day session bound documented in
  // docs/oauth.md has to mean the same thing over MCP as it does in the console.
  if (!props?.session) {
    throw new OAuthError('invalid_grant', {
      description: 'The Backroom staff session expired; authorize again',
      statusCode: 400,
    })
  }
  // The token can never outlive the session. Workers KV will not accept an
  // expiration under MIN_ACCESS_TOKEN_TTL_SECONDS, so a session with less life
  // than that cannot be represented by a token that dies with it: refuse the
  // exchange instead of rounding the token's life up past the session's.
  const remainingSeconds = Math.floor((props.session.expiresAt - now) / 1_000)
  if (remainingSeconds < MIN_ACCESS_TOKEN_TTL_SECONDS) {
    throw new OAuthError('invalid_grant', {
      description: 'The Backroom staff session is about to expire; authorize again',
      statusCode: 400,
    })
  }
  const scopes = [
    ...new Set(
      options.requestedScope.filter(
        (scope) => options.scope.includes(scope) && props.scopes.includes(scope),
      ),
    ),
  ]
  const accessTokenTTL = Math.min(MAX_ACCESS_TOKEN_TTL_SECONDS, remainingSeconds)
  const accessTokenProps: McpGrantProps = {
    session: props.session,
    scopes,
    clientName: props.clientName,
    grant: { id: options.grantId, clientId: options.clientId },
    accessExpiresAt: new Date(now + accessTokenTTL * 1000).toISOString(),
  }
  return {
    accessTokenProps,
    accessTokenScope: scopes,
    accessTokenTTL,
  }
}

export type McpGrantListing = {
  id: string
  clientId: string
  clientName: string
  scopes: Array<string>
  requestedScopes: Array<string> | null
  accessLevel: string
  authorizedAt: string | null
  createdAt: number
  expiresAt: number | null
}

const GRANT_ID_PATTERN = /^[A-Za-z0-9_-]{8,128}$/

async function operatorSession(request: Request, env: BackroomEnv) {
  const session = await readSessionFromRequest(request, env.SESSION_SECRET)
  if (!session) return null
  try {
    accessLevelForEmail(session.email, env.BACKROOM_ADMIN_EMAILS, env.BACKROOM_BLOCKED_EMAILS)
  } catch {
    return null
  }
  return session
}

function grantListing(grant: GrantSummary): McpGrantListing {
  const metadata = (grant.metadata ?? {}) as Record<string, unknown>
  const requested = metadata.requestedScopes
  return {
    id: grant.id,
    clientId: grant.clientId,
    clientName: typeof metadata.clientName === 'string' ? metadata.clientName : 'MCP client',
    scopes: grant.scope,
    requestedScopes:
      Array.isArray(requested) && requested.every((scope) => typeof scope === 'string')
        ? (requested as Array<string>)
        : null,
    accessLevel: accessLevelForScopes(grant.scope),
    authorizedAt: typeof metadata.authorizedAt === 'string' ? metadata.authorizedAt : null,
    createdAt: grant.createdAt,
    expiresAt: grant.expiresAt ?? null,
  }
}

/** Lists the signed-in operator's own MCP grants, newest first. */
export async function listMcpGrants(
  request: Request,
  env: BackroomEnv & { OAUTH_PROVIDER: OAuthHelpers },
) {
  const session = await operatorSession(request, env)
  if (!session) return noStoreJson({ error: 'Your Backroom session expired' }, 401)
  const grants: Array<McpGrantListing> = []
  let cursor: string | undefined
  do {
    const page = await env.OAUTH_PROVIDER.listUserGrants(session.uid, { cursor, limit: 100 })
    grants.push(...page.items.map(grantListing))
    cursor = page.cursor
  } while (cursor)
  grants.sort((left, right) => right.createdAt - left.createdAt)
  return noStoreJson({ grants })
}

/**
 * Revokes one of the signed-in operator's own MCP grants and every access and
 * refresh token issued under it. The grant key is namespaced by the session's
 * uid, so an operator can only ever revoke their own connections.
 */
export async function revokeMcpGrant(
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
  const session = await operatorSession(request, env)
  if (!session) return noStoreJson({ error: 'Your Backroom session expired' }, 401)
  const input = z
    .object({ grantId: z.string().regex(GRANT_ID_PATTERN) })
    .safeParse(await request.json().catch(() => null))
  if (!input.success) return noStoreJson({ error: 'Invalid grant id' }, 400)
  await env.OAUTH_PROVIDER.revokeGrant(input.data.grantId, session.uid)
  return noStoreJson({ revoked: input.data.grantId })
}
