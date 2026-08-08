import type { AuthRequest, ClientInfo, OAuthHelpers } from '@cloudflare/workers-oauth-provider'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { BackroomSession } from '../lib/auth.types'
import { sealToken } from './crypto.server'
import {
  beginMcpAuthorization,
  completeMcpAuthorization,
  getMcpConsentDetails,
} from './mcp-oauth.server'
import {
  BACKROOM_ARTIFACT_SCOPE,
  BACKROOM_READ_SCOPE,
  BACKROOM_WRITE_SCOPE,
  type BackroomEnv,
} from './mcp.server'

const secret = 'test-backroom-session-secret-0123456789'
const origin = 'https://backroom.dittobench.ai'

const oauthRequest: AuthRequest = {
  responseType: 'code',
  clientId: 'client-123',
  redirectUri: 'http://127.0.0.1:8899/callback',
  scope: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE],
  state: 'client-state',
  codeChallenge: 'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM',
  codeChallengeMethod: 'S256',
  resource: `${origin}/mcp`,
}

const client: ClientInfo = {
  clientId: 'client-123',
  clientName: 'Codex',
  clientUri: 'https://developers.openai.com/codex',
  redirectUris: [oauthRequest.redirectUri],
  grantTypes: ['authorization_code', 'refresh_token'],
  responseTypes: ['code'],
  tokenEndpointAuthMethod: 'none',
}

function oauthHelpers(completeAuthorization = vi.fn()) {
  return {
    parseAuthRequest: vi.fn().mockResolvedValue(structuredClone(oauthRequest)),
    lookupClient: vi.fn().mockResolvedValue(client),
    completeAuthorization,
  } as unknown as OAuthHelpers
}

const session: BackroomSession = {
  version: 2,
  uid: 'staff-1',
  email: 'peyton@omniaura.ai',
  name: 'Staff User',
  picture: '',
  accessLevel: 'write',
  issuedAt: Date.now(),
  expiresAt: Date.now() + 7 * 24 * 60 * 60_000,
}

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ accessLevel: 'write' })))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Backroom MCP OAuth consent', () => {
  it('binds the authorization request to the MCP resource and grants explicit write access', async () => {
    const completeAuthorization = vi.fn().mockResolvedValue({
      redirectTo: 'http://127.0.0.1:8899/callback?code=issued&state=client-state',
    })
    const oauth = oauthHelpers(completeAuthorization)
    const beginResponse = await beginMcpAuthorization(
      new Request(`${origin}/authorize`),
      oauth,
      secret,
    )
    expect(beginResponse.status).toBe(302)

    const consentUrl = new URL(beginResponse.headers.get('location') ?? '')
    const requestToken = consentUrl.searchParams.get('request') ?? ''
    const details = await getMcpConsentDetails(requestToken, origin, secret)
    expect(details).toMatchObject({
      clientName: 'Codex',
      canRequestWrite: true,
      requestedScopes: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE],
    })

    const sessionToken = await sealToken(session, secret)
    const completeResponse = await completeMcpAuthorization(
      new Request(`${origin}/oauth/authorize/complete`, {
        method: 'POST',
        headers: {
          Origin: origin,
          'Content-Type': 'application/json',
          Cookie: `__Host-backroom_session=${sessionToken}`,
        },
        body: JSON.stringify({
          requestToken,
          csrf: details.csrf,
          decision: 'allow',
          accessLevel: 'write',
        }),
      }),
      {
        SESSION_SECRET: secret,
        // Write access is re-derived from this binding at consent time rather
        // than trusted from the session cookie, so the fixture has to carry it.
        BACKROOM_ADMIN_EMAILS: 'peyton@omniaura.ai',
        OAUTH_PROVIDER: oauth,
      } as BackroomEnv & { OAUTH_PROVIDER: OAuthHelpers },
    )

    expect(completeResponse.status).toBe(200)
    await expect(completeResponse.json()).resolves.toEqual({
      redirectTo: 'http://127.0.0.1:8899/callback?code=issued&state=client-state',
    })
    expect(completeAuthorization).toHaveBeenCalledWith(
      expect.objectContaining({
        request: expect.objectContaining({
          clientId: 'client-123',
          resource: `${origin}/mcp`,
        }),
        userId: 'staff-1',
        scope: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE],
        props: expect.objectContaining({
          scopes: [BACKROOM_READ_SCOPE, BACKROOM_WRITE_SCOPE],
          session: expect.objectContaining({
                    }),
        }),
      }),
    )
  })

  it('rejects authorization requests for a different resource audience', async () => {
    const oauth = oauthHelpers()
    vi.mocked(oauth.parseAuthRequest).mockResolvedValue({
      ...oauthRequest,
      resource: 'https://evil.example/mcp',
    })

    await expect(
      beginMcpAuthorization(new Request(`${origin}/authorize`), oauth, secret),
    ).rejects.toThrow(`The OAuth resource must be ${origin}/mcp`)
  })

  it('grants artifact downloads without granting production write access', async () => {
    const completeAuthorization = vi.fn().mockResolvedValue({
      redirectTo: 'http://127.0.0.1:8899/callback?code=issued',
    })
    const oauth = oauthHelpers(completeAuthorization)
    vi.mocked(oauth.parseAuthRequest).mockResolvedValue({
      ...oauthRequest,
      scope: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE],
    })
    const beginResponse = await beginMcpAuthorization(
      new Request(`${origin}/authorize`),
      oauth,
      secret,
    )
    const requestToken =
      new URL(beginResponse.headers.get('location') ?? '').searchParams.get('request') ?? ''
    const details = await getMcpConsentDetails(requestToken, origin, secret)
    expect(details).toMatchObject({
      canRequestArtifact: true,
      canRequestWrite: false,
    })

    const sessionToken = await sealToken(session, secret)
    await completeMcpAuthorization(
      new Request(`${origin}/oauth/authorize/complete`, {
        method: 'POST',
        headers: {
          Origin: origin,
          'Content-Type': 'application/json',
          Cookie: `__Host-backroom_session=${sessionToken}`,
        },
        body: JSON.stringify({
          requestToken,
          csrf: details.csrf,
          decision: 'allow',
          accessLevel: 'artifact',
        }),
      }),
      {
        SESSION_SECRET: secret,
        // Write access is re-derived from this binding at consent time rather
        // than trusted from the session cookie, so the fixture has to carry it.
        BACKROOM_ADMIN_EMAILS: 'peyton@omniaura.ai',
        OAUTH_PROVIDER: oauth,
      } as BackroomEnv & { OAUTH_PROVIDER: OAuthHelpers },
    )

    expect(completeAuthorization).toHaveBeenCalledWith(
      expect.objectContaining({
        scope: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE],
        props: expect.objectContaining({
          scopes: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE],
        }),
      }),
    )
  })

  it('lets a write account grant write even when the client requested read only', async () => {
    const completeAuthorization = vi.fn().mockResolvedValue({
      redirectTo: 'http://127.0.0.1:8899/callback?code=issued',
    })
    const oauth = oauthHelpers(completeAuthorization)
    vi.mocked(oauth.parseAuthRequest).mockResolvedValue({
      ...oauthRequest,
      scope: [BACKROOM_READ_SCOPE],
    })
    const beginResponse = await beginMcpAuthorization(
      new Request(`${origin}/authorize`),
      oauth,
      secret,
    )
    const requestToken =
      new URL(beginResponse.headers.get('location') ?? '').searchParams.get('request') ?? ''
    const details = await getMcpConsentDetails(requestToken, origin, secret)
    expect(details).toMatchObject({
      canRequestArtifact: false,
      canRequestWrite: false,
    })

    const sessionToken = await sealToken(session, secret)
    await completeMcpAuthorization(
      new Request(`${origin}/oauth/authorize/complete`, {
        method: 'POST',
        headers: {
          Origin: origin,
          'Content-Type': 'application/json',
          Cookie: `__Host-backroom_session=${sessionToken}`,
        },
        body: JSON.stringify({
          requestToken,
          csrf: details.csrf,
          decision: 'allow',
          accessLevel: 'full',
        }),
      }),
      {
        SESSION_SECRET: secret,
        // Write access is re-derived from this binding at consent time rather
        // than trusted from the session cookie, so the fixture has to carry it.
        BACKROOM_ADMIN_EMAILS: 'peyton@omniaura.ai',
        OAUTH_PROVIDER: oauth,
      } as BackroomEnv & { OAUTH_PROVIDER: OAuthHelpers },
    )

    expect(completeAuthorization).toHaveBeenCalledWith(
      expect.objectContaining({
        scope: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE, BACKROOM_WRITE_SCOPE],
        props: expect.objectContaining({
          scopes: [BACKROOM_READ_SCOPE, BACKROOM_ARTIFACT_SCOPE, BACKROOM_WRITE_SCOPE],
        }),
      }),
    )
  })

  it('refuses write to an account the admin binding no longer lists', async () => {
    // The level is re-derived from BACKROOM_ADMIN_EMAILS at consent time, so a
    // session cookie sealed while the account still had write cannot mint a
    // privileged grant after the address is removed. Without this the 12-hour
    // session would become a 12-hour window to hand an agent write access.
    const completeAuthorization = vi.fn().mockResolvedValue({
      redirectTo: 'http://127.0.0.1:8899/callback?code=issued',
    })
    const oauth = oauthHelpers(completeAuthorization)
    const beginResponse = await beginMcpAuthorization(
      new Request(`${origin}/authorize`),
      oauth,
      secret,
    )
    const requestToken =
      new URL(beginResponse.headers.get('location') ?? '').searchParams.get('request') ?? ''
    const details = await getMcpConsentDetails(requestToken, origin, secret)
    const sessionToken = await sealToken(
      { ...session, email: 'alan@omniaura.ai', accessLevel: 'write' },
      secret,
    )

    await completeMcpAuthorization(
      new Request(`${origin}/oauth/authorize/complete`, {
        method: 'POST',
        headers: {
          Origin: origin,
          'Content-Type': 'application/json',
          Cookie: `__Host-backroom_session=${sessionToken}`,
        },
        body: JSON.stringify({
          requestToken,
          csrf: details.csrf,
          decision: 'allow',
          accessLevel: 'write',
        }),
      }),
      {
        SESSION_SECRET: secret,
        // Write access is re-derived from this binding at consent time rather
        // than trusted from the session cookie, so the fixture has to carry it.
        BACKROOM_ADMIN_EMAILS: 'peyton@omniaura.ai',
        OAUTH_PROVIDER: oauth,
      } as BackroomEnv & { OAUTH_PROVIDER: OAuthHelpers },
    )

    expect(completeAuthorization).toHaveBeenCalledWith(
      expect.objectContaining({
        scope: [BACKROOM_READ_SCOPE],
        props: expect.objectContaining({ scopes: [BACKROOM_READ_SCOPE] }),
      }),
    )
  })
})
