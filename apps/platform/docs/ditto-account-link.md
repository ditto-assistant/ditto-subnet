# Miner ↔ Ditto account link (Sign in with Ditto)

Platform acts as an OpenID Connect relying party against Ditto
(`https://api.heyditto.ai`) using the Omni Aura-owned **DittoBench** app as the
client. The result is one row per hotkey in `miner_ditto_links`; many hotkeys
may point at the same Ditto account.

## Trust boundaries

| Fact | Proven by | Never taken from |
| --- | --- | --- |
| Hotkey | a live miner session (`ditto login`, hotkey-signed device grant) | a request body |
| Ditto user id | the `sub` of an RS256 id_token verified against `/.well-known/jwks.json` (iss, aud, exp, nonce) | the browser, the CLI, or a query string |
| Callback ↔ attempt | the hashed `state` stored on the attempt | anything else in the callback |
| Return URL | `DITTO_LINK_RETURN_URL` origin only | a caller-supplied absolute URL |

The client secret lives only in `DITTO_LINK_CLIENT_SECRET` and is sent only in
the token request to the configured issuer. It is never logged or served.

## Flow

1. `POST /api/v1/me/ditto-link/start` (bearer = miner session, scope
   `profile`) creates a `miner_ditto_link_attempts` row: hashed state, nonce,
   PKCE verifier, client (`dashboard` | `cli`), a same-origin `return_to`, a
   10-minute expiry. Returns `authorize_url` on Ditto.
2. The browser consents on Ditto; Ditto redirects to
   `GET /api/v1/miner-auth/ditto/callback?code&state` (public).
3. The callback locks the attempt by state, marks it consumed **before** the
   network call (a replay can never redeem twice), exchanges the code with
   `client_id` + `client_secret` + `code_verifier`, verifies the id_token,
   upserts `miner_ditto_links` for the attempt's hotkey (recording the bound
   coldkey when attestation knows one), and redirects to
   `return_to#/reviews?ditto=linked` or `?ditto=error&reason=…`.
4. `GET /api/v1/me/ditto-link` shows the link; `DELETE` revokes it;
   `GET /api/v1/me/ditto-link/attempts/{id}` lets the CLI poll.

## Configuration

| Env | Default | Meaning |
| --- | --- | --- |
| `DITTO_LINK_ENABLED` | `false` | Off → every link route answers 503; nothing else changes. |
| `DITTO_LINK_ISSUER` | `https://api.heyditto.ai` | Ditto OpenID issuer. |
| `DITTO_LINK_CLIENT_ID` | — | DittoBench `app_id`. Required when enabled. |
| `DITTO_LINK_CLIENT_SECRET` | — | DittoBench app secret. Required when enabled. |
| `DITTO_LINK_REDIRECT_URL` | — | Exact verified callback, e.g. `https://dittobench.ai/api/v1/miner-auth/ditto/callback`. |
| `DITTO_LINK_RETURN_URL` | `https://dittobench.ai/#/reviews` | Where the browser lands afterwards. |
| `DITTO_LINK_SCOPES` | `openid email` | Requested scopes. No memory or spend scope is requested. |

Register the callback origin on the app first (`heyditto apps origins verify
--app <id> https://dittobench.ai`), then set the four `DITTO_LINK_*` values
and restart. `check_config` refuses a half-configured deployment.

## What the link is used for

- **Relay attribution** (`services/model-relay`, config-gated, off by
  default): when the Ditto Router upstream is enabled, a grant whose agent's
  miner hotkey has an active link is forwarded with
  `X-Ditto-On-Behalf-Of: <ditto_user_id>` so Ditto's ledger names the
  consenting account. No link → the centralized OpenRouter path, unchanged.
- **Feedback Track**: a contribution recorded for a Ditto user resolves to
  the hotkeys linked to that account. Recording is plumbing; no reward
  economics are defined here.

Consenting and non-consenting users, revoked sessions, cross-hotkey reads,
replayed callbacks and disabled deployments are covered by
`ditto/tests/api_server/endpoints/test_miner_ditto_link.py`.
