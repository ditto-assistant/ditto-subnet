# Verified GitHub contributor identity

A verified link binds one miner hotkey to one numeric GitHub user id. The login
is a display label captured at linking; re-link after a rename. This identity
never chooses a payment destination or changes a claim's signed payee.

Legacy GitHub accounts can have consecutive or trailing hyphens in their login.
Linking accepts these existing accounts; account-creation rules do not replace
GitHub's OAuth-proven numeric identity.

## Miner CLI

```bash
uv run ditto login
uv run ditto link-github --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY
uv run ditto link-github status
uv run ditto link-github --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY unlink
```

The saved miner session must have `read` and `profile` scopes. The command opens
an opaque dittobench.ai launch URL. Use `--no-browser` on a headless machine.
Platform creates OAuth state and PKCE in the browser, exchanges the code
server-side, reads GitHub's public profile, and discards its access token. The
CLI receives no GitHub access token, OAuth state, PKCE verifier or app secret.
No PAT is required.

The launch capability travels in a URL fragment, which is not sent in HTTP
URLs or access logs. The browser clears that fragment from history immediately
and submits it in a same-origin POST body. JavaScript must be enabled. Visiting
the clean launch URL alone cannot start or consume an attempt.

In the browser, review the GitHub login, numeric id and full hotkey, then approve
that exact pairing. Back in the CLI, the verified account is shown before the
wallet is loaded and the hotkey (or its owner coldkey with `--key-kind coldkey`)
signs. Only then is the link recorded. Cancelling either side records no link.
`--yes` skips only the CLI's pairing prompt; it does not bypass browser consent.
Whoever opens the launch URL first chooses the approving account, and a headless
run prints that URL to its logs, so `--yes`, or a `LINK` piped in without a
terminal, requires `--expect-github-id ID` and refuses any other account. Linking
replaces an existing link, including one to the same account; the CLI names the
link it will revoke before signing.

Approving lets the hotkey claim GitHub bounty credit as that login, so the
consent page warns to approve only a link started from your own CLI.

An SN118 UID is not required. `Owner(hotkey)` must resolve to a nonzero coldkey
at a finalized block. Coldkey proofs must use that owner, not a payment record
or a client-supplied coldkey. Each hotkey and each GitHub id has at most one
active link globally, including across netuids, enforced by partial unique
database indexes. A netuid binds the signed proof; it does not permit another
active identity mapping. Lookups, re-linking and signed unlinking use this global
mapping even when the active proof was recorded on another netuid; its original
signed fields remain unchanged. Re-linking appends a new proof and revokes the
previous link. Signed unlinking names the exact active
link; it preserves all proof/history rows. Proof fields cannot be edited, and
revocation cannot be undone.

The action ledger records the netuid used by each signature, including a
revocation signed on a different netuid from the original link proof. This
keeps both signed messages independently reconstructible from stored history.

The signing contracts are canonical shared code in
`ditto_screening_protocol.contributor_identity`. Payloads bind the domain,
netuid, hotkey, exact numeric id/login, attempt id, nonce, timestamp, key kind
and signer. Nonces are unique across link/unlink actions. The owner-link
freshness limits apply: at most 24 hours old or five minutes in the future.

## Deployment

Register a dedicated GitHub OAuth app for public-profile identity only, with
callback `https://dittobench.ai/api/v1/miner-auth/github/callback`. Configure
`GITHUB_LINK_ENABLED=true`, `GITHUB_LINK_CLIENT_ID`, the server-only
`GITHUB_LINK_CLIENT_SECRET`, and `GITHUB_LINK_REDIRECT_URL` on Platform.
Keep the secret in the deployment's secret store, never GitHub variables, URLs
or the CLI. Linking defaults off and fails clearly until configured; existing
verified links remain readable when the OAuth client is disabled.

Ansible owns these settings in `platform_app`; dev remains disabled. Terraform
owns `platform-github-link-client-secret` and grants only the attached Platform
runtime identity access. Add the OAuth secret payload directly to Secret Manager
after the reviewed infrastructure plan is applied; never put it in Terraform
variables or state. The production OAuth app is **DittoBench Contributor Identity**,
owned by `ditto-assistant` (application id `3947441`).

For day-two activation, run from `infra/ansible`:

```bash
GCP_OSLOGIN_USER=YOUR_OS_LOGIN ansible-playbook -i inventory/gcp.yml \
  playbooks/gcp-platform-github-link.yml --limit ditto-platform-prod --check
GCP_OSLOGIN_USER=YOUR_OS_LOGIN ansible-playbook -i inventory/gcp.yml \
  playbooks/gcp-platform-github-link.yml --limit ditto-platform-prod
```

This playbook requires an existing `.env`, backs it up, and changes only the
managed GitHub linking block. It does not restart services. Deploy the exact
reviewed revision through `platform-deploy.yml` after convergence; confirm its
SHA at `/health` and complete an OAuth consent + wallet signature link from the
CLI. A successful callback/configuration check alone is not a completed link.
To roll back activation, set `platform_github_link_enabled: false`, converge
the same playbook, and redeploy. Existing links remain readable.

The web flow requests an empty scope and uses S256 PKCE. A returned token with
broader historical scopes is refused; use a dedicated app rather than reusing
an app that requested repository or private-profile access. See GitHub's
[OAuth web flow](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)
and [authenticated-user API](https://docs.github.com/en/rest/users/users#get-the-authenticated-user).

## Public verification and bounty integration

`GET /api/v1/public/github-link/{hotkey}` returns the active proof, including
its signing inputs and finalized ownership snapshot. Numeric account lookup
is `GET /api/v1/public/github-account/{github_user_id}`. Both are `no-store`.
Backroom provides `get_miner_github_link` and `get_github_contributor_status`
with `backroom:read`. The latter combines the linked hotkey with its CLA status.

Bounty claiming remains a proposed subsystem (#2045 / #2074). Its acceptance
flow must call `prepare_bounty_identity` from
`apps/platform/ditto/api_server/bounty_identity.py` after verifying the signed
claim, before opening its acceptance transaction. This releases the temporary
proof-read connection before contacting GitHub. Pass that helper's in-process
evidence to `require_verified_bounty_contributor` inside the transaction that
records the claim; never construct evidence from client JSON. Evidence expires
after two minutes. The guard rejects absent links, mismatched
logins, renamed/reassigned logins, unavailable GitHub evidence and missing
current consent when CLA enforcement is enabled. It checks the public login's
current numeric id against the OAuth-proven id.

The fresh public-data request uses the OAuth app's server-only client credentials
under GitHub's [app rate budget](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api#primary-rate-limit-for-oauth-apps).
It requires configured app credentials and never caches a positive identity;
renaming or reassignment is checked again for each prepared claim.

Call the guard in the same transaction
that records the claim: link revocation and policy changes are serialized with
that transaction. Persist the returned link id, numeric user id and consent
receipt id for audit. The result intentionally contains no payee field.

For a GitHub PR check, take the numeric author id from the trusted GitHub event,
resolve its linked hotkey, then check that hotkey's current CLA receipt. Never
resolve an author solely by a login string. This read interface is ready for a
follow-up check; it does not award a bounty or authorize payment.
