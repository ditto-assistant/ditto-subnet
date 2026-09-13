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
| Return URL | `DITTO_LINK_RETURN_URL` origin only, re-serialised, backslashes/userinfo/control chars refused, `ditto`/`reason`/`attempt` stripped | a caller-supplied absolute URL |
| Pairing hotkey ↔ account | two-sided: the signed-in Ditto browser accepts the named hotkey (single-use token), then the miner session that started the attempt confirms | either party alone; the browser that happened to complete the callback |
| Dashboard attempts | HttpOnly `SameSite=Lax` cookie bound to the attempt, required at the callback | an authorize URL forwarded to another browser |

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
   `client_id` + `client_secret` + `code_verifier`, verifies the id_token and
   parks the verified identity as `identity_verified` with a **single-use
   accept token** (sha256 at rest, 10-minute TTL). Nothing is linked and the
   hotkey side is told nothing about who signed in. For a dashboard-started
   attempt the callback also requires the HttpOnly `SameSite=Lax` cookie set at
   `/start`, so a dashboard authorize URL handed to someone else fails closed.
   The signed-in browser is redirected to `GET /api/v1/miner-auth/ditto/accept`.
4. **Ditto-side acceptance.** That page names the hotkey (and coldkey) asking
   for the account and offers *Yes, link* / *Not me*. `POST …/accept` consumes
   the token: accept → `authenticated` (identity now visible to the hotkey
   side), decline → `failed`. This is what stops the reverse phish: a miner
   who tricks someone else into signing in on their attempt gets a declined
   attempt, not that person's account.
4b. `POST /api/v1/me/ditto-link/attempts/{id}/confirm` — bearer = the miner
   session that **started** the attempt (scope `profile`), attempt must be
   `authenticated` and unexpired — writes `miner_ditto_links` (recording the
   bound coldkey when attestation knows one). Two-sided: the account holder
   accepted the hotkey, the hotkey holder confirms the account.
5. `GET /api/v1/me/ditto-link` shows the link; `DELETE` revokes it;
   `GET /api/v1/me/ditto-link/attempts/{id}` lets the dashboard and CLI poll
   (it carries the parked identity once authenticated).

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

## Feedback Track plumbing

`feedback_track_contributions` records, per Ditto account, that a feedback
report was filed (`report`), followed up (`follow_up`) or shipped (`shipped`),
keyed idempotently by `(source, external_ref, kind)`.

- `POST /api/v1/feedback-track/contributions` — operator bearer
  (`DITTO_ADMIN_API_TOKEN`), written by the Ditto backend; replays return 200
  with `created: false`. `X-Admin-Actor` names the writer.
- `GET /api/v1/me/feedback-track` — the signed-in miner's contributions through
  the account linked to their hotkey; `linked: false` when there is no link.
- `GET /api/v1/public/feedback-track/{hotkey}` — counts by kind only; never
  the account id, email, or report text. Revoking the link zeroes the public
  view without deleting the record.

`weight` is stored and consumed by nothing. No reward policy, emission split or
scoring input is defined by this table; that decision belongs to a separate,
reviewed change.
