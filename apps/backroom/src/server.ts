import handler from '@tanstack/react-start/server-entry'
import OAuthProvider from '@cloudflare/workers-oauth-provider'
import {
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_CHALLENGE_SCOPE,
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  type BackroomEnv,
} from './server/mcp.server'
import { BackroomMcpHandler } from './server/mcp-handler.server'
import {
  beginMcpAuthorization,
  completeMcpAuthorization,
  listMcpGrants,
  MAX_ACCESS_TOKEN_TTL_SECONDS,
  mcpTokenExchange,
  revokeMcpGrant,
} from './server/mcp-oauth.server'
import { cacheOAuthTokenReads } from './server/oauth-token-cache.server'
import { SESSION_MAX_AGE_SECONDS } from './lib/auth.policy'

const defaultHandler = {
  async fetch(request: Request, env: BackroomEnv) {
    const url = new URL(request.url)
    const oauth = env.OAUTH_PROVIDER
    if (!oauth) {
      return Response.json(
        { error: 'server_error', error_description: 'OAuth is unavailable' },
        { status: 500 },
      )
    }
    if (url.pathname === '/authorize') {
      if (request.method !== 'GET') {
        return new Response('Method not allowed', { status: 405 })
      }
      try {
        return await beginMcpAuthorization(request, oauth, env.SESSION_SECRET)
      } catch (cause) {
        return Response.json(
          {
            error: 'invalid_request',
            error_description:
              cause instanceof Error ? cause.message : 'Invalid OAuth authorization request',
          },
          { status: 400, headers: { 'Cache-Control': 'no-store' } },
        )
      }
    }
    if (url.pathname === '/oauth/authorize/complete') {
      if (request.method !== 'POST') {
        return new Response('Method not allowed', { status: 405 })
      }
      try {
        return await completeMcpAuthorization(request, {
          ...env,
          OAUTH_PROVIDER: oauth,
        })
      } catch (cause) {
        console.error('MCP authorization completion failed', cause)
        return Response.json(
          {
            error: 'server_error',
            error_description: 'Unable to complete MCP authorization',
          },
          { status: 500, headers: { 'Cache-Control': 'no-store' } },
        )
      }
    }
    if (url.pathname === '/oauth/grants' || url.pathname === '/oauth/grants/revoke') {
      const expectedMethod = url.pathname === '/oauth/grants' ? 'GET' : 'POST'
      if (request.method !== expectedMethod) {
        return new Response('Method not allowed', { status: 405 })
      }
      const grantEnv = { ...env, OAUTH_PROVIDER: oauth }
      try {
        return url.pathname === '/oauth/grants'
          ? await listMcpGrants(request, grantEnv)
          : await revokeMcpGrant(request, grantEnv)
      } catch (cause) {
        console.error('MCP grant management failed', cause)
        return Response.json(
          { error: 'server_error', error_description: 'Unable to manage MCP grants' },
          { status: 500, headers: { 'Cache-Control': 'no-store' } },
        )
      }
    }
    if (url.pathname === '/.well-known/oauth-authorization-server/mcp') {
      return Response.json({
        issuer: url.origin,
        authorization_endpoint: `${url.origin}/authorize`,
        token_endpoint: `${url.origin}/token`,
        registration_endpoint: `${url.origin}/register`,
        revocation_endpoint: `${url.origin}/token`,
        response_types_supported: ['code'],
        response_modes_supported: ['query'],
        grant_types_supported: ['authorization_code', 'refresh_token'],
        token_endpoint_auth_methods_supported: [
          'client_secret_basic',
          'client_secret_post',
          'none',
        ],
        code_challenge_methods_supported: ['S256'],
        scopes_supported: [
          BACKROOM_READ_SCOPE,
          BACKROOM_ARTIFACT_SCOPE,
          BACKROOM_WRITE_SCOPE,
        ],
        client_id_metadata_document_supported: true,
      })
    }
    if (url.pathname === '/.well-known/mcp/server.json') {
      return Response.json({
        name: 'ai.dittobench/backroom',
        description:
          'OAuth-protected operator control for Bittensor SN118: screening quarantines, validator queue and slot policy, benchmark rollouts, scoring policy, and the emission burn.',
        version: '1.0.0',
        remotes: [
          {
            type: 'streamable-http',
            url: `${url.origin}/mcp`,
          },
        ],
      })
    }
    return handler.fetch(request)
  },
}

const oauthProvider = new OAuthProvider<BackroomEnv>({
  apiRoute: '/mcp',
  apiHandler: BackroomMcpHandler,
  defaultHandler,
  authorizeEndpoint: '/authorize',
  tokenEndpoint: '/token',
  clientRegistrationEndpoint: '/register',
  scopesSupported: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE, BACKROOM_WRITE_SCOPE],
  resourceMetadata: {
    scopes_supported: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE, BACKROOM_WRITE_SCOPE],
    bearer_methods_supported: ['header'],
    resource_name: 'SN118 Backroom MCP',
  },
  allowImplicitFlow: false,
  allowPlainPKCE: false,
  allowTokenExchangeGrant: false,
  disallowPublicClientRegistration: false,
  clientIdMetadataDocumentEnabled: true,
  accessTokenTTL: MAX_ACCESS_TOKEN_TTL_SECONDS,
  refreshTokenTTL: SESSION_MAX_AGE_SECONDS,
  clientRegistrationTTL: 90 * 24 * 60 * 60,
  // Tokens never outlive the authorizing staff session, carry only scopes the
  // grant's consent recorded, and name their exact grant (see mcpTokenExchange).
  tokenExchangeCallback: (options) => mcpTokenExchange(options),
  onError({ status, code, description, internal }) {
    console.warn('Backroom OAuth error', {
      status,
      code,
      description,
      internal,
    })
  },
})

function applySecurityHeaders(response: Response, request: Request) {
  const secured = new Response(response.body, response)
  secured.headers.set('X-Content-Type-Options', 'nosniff')
  secured.headers.set('Referrer-Policy', 'no-referrer')
  if (new URL(request.url).protocol === 'https:') {
    secured.headers.set(
      'Strict-Transport-Security',
      'max-age=31536000; includeSubDomains',
    )
  }
  return secured
}

export default {
  async fetch(request: Request, env: BackroomEnv, ctx: ExecutionContext) {
    const oauthEnv = cacheOAuthTokenReads(env, request, ctx)
    const response = await oauthProvider.fetch(request, oauthEnv, ctx)
    if (new URL(request.url).pathname === '/mcp' && response.status === 401) {
      const challenged = new Response(response.body, response)
      const challenge = challenged.headers.get('WWW-Authenticate') ?? 'Bearer'
      if (!challenge.includes('scope=')) {
        challenged.headers.set(
          'WWW-Authenticate',
          `${challenge}, scope="${BACKROOM_CHALLENGE_SCOPE}"`,
        )
      }
      return applySecurityHeaders(challenged, request)
    }
    return applySecurityHeaders(response, request)
  },
  async scheduled(
    _controller: ScheduledController,
    env: BackroomEnv,
    ctx: ExecutionContext,
  ) {
    ctx.waitUntil(oauthProvider.purgeExpiredData(env, { batchSize: 100 }))
  },
} satisfies ExportedHandler<BackroomEnv>
