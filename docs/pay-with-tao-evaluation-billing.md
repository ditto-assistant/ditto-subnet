# Pay with TAO: miner evaluation billing

Tracking: [DITTO-75](https://omniaura-team.atlassian.net/browse/DITTO-75).
This is the subnet integration checkpoint for product credit purchases.
It does not activate production billing or claim every evaluation cost is paid
by the miner.

## Account and credit boundary

The existing two-sided Sign in with Ditto flow proves the hotkey from the miner
session and the Ditto account from a verified OIDC subject. Neither a checkout
query parameter nor an inference header supplied by a miner can select the
account to credit or bill. Payment deposits belong to the authenticated Ditto
account; multiple hotkeys can link to that same account.

The Ditto top-up route is `/settings/usage?funding=tao`. A browser may already be
signed into a different Ditto account: an ordinary hyperlink does not prove its
session matches the miner's linked subject. Until there is a signed handoff,
show/check the linked account and the account named at checkout. Do not add a
caller-supplied `uid` as a substitute for authentication.

## Opt-in competition chat requirement

`DITTO_ROUTER_REQUIRE_COMPETITION_ACCOUNT` defaults to `false`. When enabled:

- The relay refuses to boot unless the Ditto Router upstream and competition
  lane are configured. The existing locked-model, memories-off acknowledgement
  still applies.
- Platform refuses to boot with linking disabled, and appends `credits:spend`
  to the OAuth scopes requested for **new** sign-ins. Identity-only linking
  retains its existing `openid email` defaults when the flag is off. Existing
  links require a new consent flow; this code never manufactures a grant.
- After checking the signed inference proof and before reserving request/token
  capacity, the relay resolves the agent's active account link from Postgres.
  Missing/revoked links return 402; lookup failures return 503. Neither invokes
  OpenRouter as a sponsor fallback.
- The Python competition-chat route, which Caddy can reach when relay ports
  are unset or rolled back, returns 503 for **every** identity when the flag
  is enabled. It has no account-billed upstream and refuses before account,
  grant, reservation or provider access. Losing the Go route cannot silently
  revert to sponsor spending.
- Linked requests go to Ditto Router, which remains responsible for verifying
  the live app spending grant, account funds, caps, and the app endpoint's
  billing mode. The flag alone cannot turn a sponsor-billed endpoint into a
  user-billed endpoint. A Router 402 returns 402 to the broker and settles no
  successful usage; it is never retried against the sponsor.

Ansible renders the flag from
`platform_ditto_router_require_competition_account: false`. No host variable
changes activate it in this PR. All activation still requires the independent
OIDC origin, user consent, user-billed endpoint, and funded/empty/unconsented
acceptance checks in
[backend#2695](https://github.com/ditto-assistant/backend/issues/2695).

The relay adds these outbound headers from the verified grant/request:

| Header | Source |
| --- | --- |
| `X-Ditto-On-Behalf-Of` | Active linked OIDC subject in Postgres |
| `X-Ditto-Evaluation-Grant` | Inference grant UUID |
| `X-Ditto-Evaluation-Request` | Signed request nonce UUID |
| `X-Ditto-Evaluation-Agent` | Grant's agent UUID |
| `X-Ditto-Evaluation-Stage` | Server constant `competition-chat` |

These are correlation inputs. The backend must persist/echo them on its
authoritative billing receipt before claiming a joined run/stage cost ledger.
They are not an idempotency promise from the gateway, and ambiguous provider
transport outcomes are not automatically re-sent by the relay.

## Evaluation stage coverage

The frozen confirmation profile remains unchanged. Its source is
`packages/ditto-screening-protocol/ditto_screening_protocol/data/confirmation_execution_profile_v9_shadow.json`.

| Stage | Current frozen route | This checkpoint |
| --- | --- | --- |
| Competition chat | Grant-locked model; optional Ditto Router upstream | Required account gate and trusted attribution; real debit requires user-billed app endpoint and live spend consent |
| Competition embeddings | `perplexity/pplx-embed-v1-0.6b`, 768 dimensions, Perplexity; OpenRouter/direct fallback policy | Existing sponsor route; not covered by the competition chat flag |
| Confirmation reader | `openai/gpt-oss-20b`, throughput with ZDR and no fallback | Existing frozen route; no user debit integration |
| Confirmation judge | `deepseek/deepseek-v4.1-flash`, Together, receipt provider `Together` | Existing frozen route; no user debit integration |
| Confirmation embeddings | `perplexity/pplx-embed-v1-0.6b`, 768 dimensions, Perplexity | Existing frozen route; no user debit integration |

Do not label this flag "miner-paid evaluation" or enable it as a substitute for
full stage coverage. Full paid evaluation requires either a trusted,
consent-checked reserve/settle billing API around these existing provider calls,
or Router endpoints preserving every stage's exact model/provider/profile.
The latter also requires an authenticated, user-billed embeddings operation
(the product Router currently exposes no public `/v1/embeddings` route) and
authoritative provider/cost metadata for frozen confirmation settlement.

Before complete activation, prove each stage reserves a bounded amount before
billable work, settles actual cost on the linked user's canonical credit
ledger, joins a run/stage receipt, and refuses empty or unconsented accounts
without falling back to Platform funds. Replays, uncertain outcomes,
concurrent evaluations and account unlinking need their own failure tests.
Code-level routing fixtures do not establish real provider charges or account
debits.

## Submission access and inference are separate charges

The submission fee remains a separate, low admission/spam fee. Inference and
evaluation usage are metered against the miner's Ditto credits in addition to
that fee. This checkpoint does not change the live submission amount, remove
payment-proof checks, or waive the fee when an account is linked or funded.
No new numeric admission fee has been selected.

Complete miner-paid evaluation must show an upfront usage estimate and an
enforced spending cap. Receipts and economic reporting must distinguish
submission access fees from actual inference debits; a top-up is prepaid
credit, not proof that an evaluation consumed it. This distinction is needed
to compare predictable per-run operating costs with the separate access fee.

## Treasury boundary

Deposits, conversion to DITTO, and staking under a dedicated payment treasury
belong to the product payment rail. The Gamma collector/GM holding wallets are
separate operational identities and are not automatically payment treasury
accounts. Production validator identity is supplied as `VALIDATOR_PROD_HOTKEY`
to the validator playbook; the committed dev validator hotkey is not evidence
of the requested production staking destination. Verify the exact public
treasury coldkey and validator hotkey before any real transfer or stake.
