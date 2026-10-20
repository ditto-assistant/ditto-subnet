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
   for the account and offers *Yes, link* / *Not me*. `POST …/accept` takes
   the token from the form body (never its URL) and consumes it under a row
   lock: accept → `authenticated` (identity now visible to the hotkey side),
   decline → `failed`. This is what stops the reverse phish: a miner who
   tricks someone else into signing in on their attempt gets a declined
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
| `DITTO_LINK_SCOPES` | `openid email connection:github:read` | Identity plus GitHub connection metadata; no memory, tool or spend access. |

Register the callback origin on the app first (`heyditto apps origins verify
--app <id> https://dittobench.ai`), then set the four `DITTO_LINK_*` values
and restart. `check_config` refuses a half-configured deployment.

## What the link is used for

Connect Ditto is the main onboarding flow. GitHub must already be connected in
the consenting Ditto account; Ditto's generic prerequisite screen offers the
connection when it is missing. After code exchange, Platform checks
`/oauth/connections` using the access token and requires the response subject
to match the verified ID-token subject. Tokens are neither stored nor returned.
An unavailable provider or mismatched identity stops the attempt.

For the GitHub proof, Platform creates a parallel GitHub attempt with the same
server-generated UUID as the Ditto attempt. This equality records provenance;
neither UUID is caller-selected. The shared browser acceptance approves it,
and the signed GitHub proof endpoint additionally requires the original
Ditto attempt to be linked by its starting miner session. The CLI signs the
existing canonical GitHub payload and uses the unchanged finalized owner,
nonce, global numeric GitHub uniqueness and immutable history checks. A matching
existing proof is kept. Direct GitHub OAuth and old proofs remain compatible.

Dashboard users finish the wallet signature with
`ditto link-github --ditto-attempt <id>`; `ditto link-ditto` does this in the
same command. Unattended signing still requires a pinned `--expect-github-id`.
The short-lived proof attempt expires after ten minutes. Multiple hotkeys can
share a Ditto router account, while a numeric GitHub identity retains its
single active bounty hotkey. Connecting Ditto does not enable credit spending.

Deploy the backend/API and consent UI first. An explicit legacy
`DITTO_LINK_SCOPES=openid email` preserves identity-only linking during a staged
rollout; remove that override when the prerequisite API and UI are ready.

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

## Activation (Ansible, land-inactive)

Everything ships OFF. `infra/ansible/roles/platform_app` renders the variables
below into `apps/platform/.env` (shared by the API and the relay pool);
activation is a `host_vars` change followed by a converge, in this order:

1. **Prove the origin.** `heyditto apps origins verify <app> https://dittobench.ai`
   returns a challenge token; set `platform_ditto_callback_challenge_token` to it,
   converge, check `curl https://dittobench.ai/.well-known/ditto-callback-challenge`
   returns the token, then `heyditto apps origins verify … --confirm`. Linking stays
   off during this step.
2. **Land the secrets.** `heyditto apps secret rotate <app> --gcp-secret <id> --project ditto-app-dev`
   forwards the new secret straight into Secret Manager (nothing is printed); set
   `secret_ditto_link_client_secret: <id>`. For the relay, mint a key on the
   sponsor-billed DittoBench endpoint into Secret Manager and set `secret_ditto_router_api_key`.
3. **Switch on.** `platform_ditto_link_enabled: true`, `platform_ditto_link_client_id`,
   `platform_ditto_link_redirect_url: https://dittobench.ai/api/v1/miner-auth/ditto/callback`;
   converge; `GET /api/v1/me/ditto-link` reports `enabled: true`. For the relay,
   `platform_ditto_router_upstream_enabled: true`, `platform_ditto_router_lanes: competition`
   and the exact ack `platform_ditto_router_competition_ack: memories-off-locked-model`,
   after a canary parity sample.

User billing on the DittoBench endpoint stays `sponsor` until the acceptance run in
backend#2695 passes with a real consenting account.
