# SN118 Backroom MCP

`get_validator_weight_diagnostics` reads revealed weights, last Yuma vTrust,
consensus, epoch counters, and pending timelock rounds at one chain block/hash.
It requires `backroom:read`, accepts an optional `validatorUid`, and never submits
weights or returns ciphertext. Current revealed rows can postdate the last Yuma
calculation; historical clipping and sustained recovery require multiple
epoch-bound observations. Chain read failures are errors, not healthy empty rows.
Stored weights are max-scaled u16 values; divide by each row's sum when comparing
allocation shares, rather than dividing each weight by 65535.

`next_epoch_block` is simulated from `LastEpochBlock`, `PendingEpochAt`, `Tempo`,
and `BlocksSinceLastStep` with the same `should_run_epoch` port drand 2.0 and the
Pylon image use. Each pending commit carries `implied_reveal_block`, recovered
from its drand round and the commit block's `Timestamp.Now` to about one block,
and `implied_reveal_offset_blocks` relative to the boundary that ends the
commit's epoch. About `+3` is the stateful schedule; a large negative offset is
the legacy `tempo+1` same-epoch lane. This is how the reveal schedule of every
validator, including those not on our stack, is compared, and the post-rollout
check that each managed validator lands at boundary `+3` rather than at the
boundary itself. A null implied block means the commit block's timestamp state
was unreadable on the node, not a healthy result.

`get_outlier_escalation` reads the anomalous-score escalation that can open ATH
holds (`review_kind` `anomalous_score`) through
`GET /api/v1/admin/outlier-escalation`. The escalation is configured only by
`DITTO_OUTLIER_ESCALATION_*` variables that each Platform process reads once at
startup, so this is where to check the effective mode (`off`, `observe` or
`enforce`) and thresholds. `sources` marks each value `env`, `default`, or
`default_invalid_env`: the variable was set, rejected, and replaced by the
shipped default. The rejected text is never returned. Activity comes from the
append-only score audit chain. It gives exact observe and enforce counts over
all time and over the last 168 hours, the 20 newest entries (`recent_truncated`
flags older ones), and the count of pending outlier ATH reviews. The tool is
read-only. It is not `/admin/score-outliers`, which covers validator
disagreement inside one quorum.

`https://backroom.dittobench.ai/mcp` is an OAuth-protected Streamable HTTP MCP
server exposing the same operations as the console: screening quarantines and
disputes, validator queue/slot/inference policy, benchmark rollouts, scoring
policy, scores and leaderboards, and the emission burn.

It was ported from the private `ditto-assistant/backroom` repository, which
keeps only `backroom.heyditto.ai` and the Ditto app surface. Feature flags and
app reviews are deliberately **not** served here: they reach the private product
API this deployment holds no credentials for. `src/lib/subnet-surface.test.ts`
enforces both halves of that boundary — the MCP must be wired, those tools must
not be.

## Authorization

`get_coding_private_v2_releases` reads native private-v2 registration digests and
quarantine/retirement audit state through Platform's existing admin endpoint.
It is distinct from the older `get_coding_catalog_releases` surface. A result
from one cannot establish the state of the other. Its bounded `limit` defaults
to 50 (maximum 100); `total` remains the full registry count, so an omitted row
outside the returned window is not evidence of absence.

The read tool strips unknown response fields, including nested ones, and exposes
no full publication receipts, private source, storage coordinates, wrapped keys
or credentials. The registered publication/probe/key digests are historical
commitments, not fresh connectivity or custody checks. Registration remains
non-selectable and weight-ineligible; it does not approve native execution, a
canary or a rollout.

`get_coding_control_plane` reads the contract-v1 catalog, native private-v2
registry, safe feature-gate posture, and redacted native assignment progress
together while preserving them as separate nested authorities. It is a bounded
convenience read, not a new source of truth, process-liveness proof, or
activation check.

`get_coding_control_plane` reads that registry together with the distinct
contract-v1 catalog in one bounded response. It keeps the two collections
separate and does not infer provider, host, canary or rollout readiness from a
stored registration.

Write-scoped operators can use three exact private-v2 lifecycle tools:

- `register_coding_private_v2_release` forwards a complete publication receipt,
  registration authority, curator public key and confirmation to Platform for
  full digest and Ed25519 verification. It never accepts a private key or
  provider credential.
- `quarantine_coding_private_v2_release` appends a quarantine event bound to the
  exact registration digest.
- `retire_coding_private_v2_release` appends the terminal retirement event while
  preserving the registration and publication audit.

The same control surface exposes the already implemented contract-v1 shadow
launch boundary without pretending it activates private v2:

- `reconcile_coding_shadow_artifact` prepares one exact qualified artifact and
  future-height run. It does not issue tickets or execute work.
- `issue_coding_shadow_ticket_set` issues one sorted, unique, fixed k=3 validator
  set for an already-issued run. Validators still claim and execute their own
  tickets.

Both launch tools retain Platform's default-off feature gates, exact confirmation
phrases, idempotent database authority and permanent `weight_eligible=false`.
The MCP calls carry the signed-in operator email in `X-Admin-Actor`; no shared
operator identity is used.

Shadow admission score floors remain the existing append-only
`get_core_qualification_policy` / `set_core_qualification_policy` controls.
They determine qualification only; they never rewrite validator scores. Coding
also reuses the existing screener-review, inference-admission, validator-slot and
benchmark-rollout authorities instead of creating divergent copies.

The MCP endpoint is a full OAuth 2.1 resource. `@cloudflare/workers-oauth-provider`
owns `/authorize`, `/token`, and `/register`; discovery lives at
`/.well-known/oauth-authorization-server/mcp` and `/.well-known/mcp/server.json`.
A client registers dynamically, then the operator approves the connection on
`/oauth/consent` while signed in to Backroom with Google.

Three scopes, in ascending sensitivity:

| Scope | Grants |
|---|---|
| `backroom:read` | Every read. Required for any connection. |
| `backroom:artifact:read` | Miner-submitted source and sensitive diagnostics: tarball URLs, file listings, source search, copy/baseline diffs, and sanitized private screening failure details. |
| `backroom:write` | Mutations, including `set_burn_settings`, which moves TAO. |

Two independent gates apply to every privileged call. The grant must carry the
scope, **and** the operator's account must still resolve to `write` through
`BACKROOM_ADMIN_EMAILS`. The level is re-derived from that binding at consent
time rather than read from the session cookie, so removing an address stops the
next authorization from minting a privileged grant even while a 7-day session
is still live. `mcp-scope.server.ts` additionally challenges the request with a
`WWW-Authenticate` scope hint before the tool runs, so an under-scoped client
gets a 403 naming the scope it needs rather than a tool-level refusal.

A grant is the intersection of the scopes the client requested, the level the
operator selected on consent, and the account's live level. Consent can narrow
a request but never widen it: a `scope=backroom:read` request yields a
read-only grant whatever is selected, and a client that needs more must
reconnect and request the broader scope (the step-up challenge above names it).
The unauthenticated `/mcp` 401 challenge advertises all three scopes, because
MCP clients request exactly the challenged scope: a first connection asks for
everything, and the operator narrows it on consent. The consent screen always
shows all four levels and disables any this request or account cannot receive,
with the reason.
Approving a client replaces every earlier grant that client id held, and a
token request can only downscope within its grant. `get_backroom_access`
reports the connection's exact `grant` id and client id, the token's
`grantedScopes`, and the effective `scopes` after the live-level cap. Operators
list and revoke their own grants (with every access and refresh token issued
under them) on the Agent access page, backed by `GET /oauth/grants` and
same-origin `POST /oauth/grants/revoke`.

Access tokens never outlive the operator session. `mcpTokenExchange` clamps the
token TTL to the session's exact remaining seconds (capped at 24 hours) and
answers `invalid_grant` when less than 60 seconds remain, because Workers KV
cannot express an expiry under a minute and rounding it up would outlive the
session. The MCP handler re-checks `session.expiresAt` on every request, exactly
as it re-derives the live email level, so an expired session ends read, artifact,
and write access at once. `get_backroom_access` reports that access-token
`expires_at`. Signed artifact download URLs stay on their own short lifetime
and are not extended with the session. There is no refresh path for the
identity itself — when the session ends, the operator authorizes again.

## Bindings

Beyond the console's own secrets (`docs/oauth.md`), the MCP needs:

- `OAUTH_KV` — registered clients, grants, and issued tokens. Terraform owns the
  namespace (`infra/terraform/stacks/cloudflare-dittobench`) and exports
  `backroom_oauth_kv_namespace_id`. That value belongs in the `prod` environment
  as the `BACKROOM_OAUTH_KV_ID` variable, **not** in `wrangler.jsonc`: this
  repository is public and the namespace holds live operator grants and tokens.
  `scripts/inject-oauth-kv.mjs` binds it immediately before `wrangler deploy`,
  and the committed placeholder is not a valid id, so a deploy that skips that
  step fails at Cloudflare rather than shipping a Worker that cannot persist a
  grant. `subnet-surface.test.ts` fails if a real id is ever committed.
  Deleting the namespace revokes every operator's MCP connection.
- The hourly cron trigger, which purges expired grants and tokens.

## Adding a tool

Tools wrap `admin.service.ts`, the same layer the console's server functions
call, so a new capability is one `registerTool` entry:

1. Add or reuse the service function and its zod schema in `admin.schemas.ts`.
2. Register the tool in `mcp.server.ts`. Writes go through `write(() => …)` and
   pass `props.session.email` so the platform records the real operator as the
   audit actor; artifact reads go through `artifact(() => …)`.
3. Add the name to `WRITE_TOOL_NAMES` (or `TOOL_SCOPE_REQUIREMENTS` for artifact
   scope) so the pre-flight scope challenge covers it.
4. Add it to the expected catalog list in `mcp.server.test.ts`. That list is
   exhaustive on purpose: a tool that is not named there fails the suite.

Keep the catalog description in `MCP_CATALOG_DESCRIPTIONS` short — the whole
catalog is loaded into model context before any call, and the test bounds both
the total and the per-description length. Long-form operational notes belong in
the `description` field, which `get_backroom_tool_help` serves on demand.

## Paging

A paged tool answers with `count` (the upstream total), `returned` (rows in this
response), `limit`, `offset`, and `has_more`. `has_more` is the only field that
reports MCP paging. An upstream `truncated` flag is a different fact: the
platform stopped short of a complete answer — paths it dropped before paging, or
a scan that hit its own match cap — so no later offset recovers what it covers,
and any total it accompanies is a lower bound. The two are not interchangeable,
and neither one substitutes for the other.

Where the window is resolved differs by tool. `search_screening_source` pages on
the platform, which holds the whole ordered match list, so the Worker must not
re-slice a window it never had. A collection the platform returns whole is paged
here by `paginateLocalCollection`, which is what emits `returned` and `has_more`.

Default page sizes are bounded so one call cannot flood model context, but a
manifest an operator reads to decide *what exists* — today
`list_screening_source_files` — defaults to the platform's whole listing rather
than a page, because a row silently missing from page one is evidence the
reviewer never learns to ask for. `mcp.server.test.ts` pins each tool's bound.

## Independent V13 replay process identity

`get_screener_replay_process_readiness` reads the exact node-2 public-key
fingerprint, signed worker-1 heartbeat, minimum runner release, and missing
admission checks from Platform. A healthy ordinary screener heartbeat is not a
signed replay-process heartbeat. This read does not prove physical host
isolation or activate replay.

`register_screener_replay_process_key` accepts only a host-generated Ed25519
**public** key for `subnet-screener-2-worker-1`. Platform requires the enrolled
node's expected hotkey, replay capacity zero, an audit reason, and the exact
confirmation containing the SHA-256 of the 32-byte public key. Never send the
private key to Backroom. `revoke_screener_replay_process_key` binds the active
key fingerprint and expected hotkey; it remains available during a live canary
so a compromised or stale process can be stopped. Both writes require
`backroom:write` and forward the signed-in operator email as `X-Admin-Actor`.
No key registration, capacity change, or live host enrollment is performed by
these tools merely becoming available.

## Finding a submission

`search_submissions` resolves what an operator knows (an agent name or name
prefix, a miner hotkey or payment coldkey, an artifact SHA-256, statuses, reason
codes, a submitted window) to exact rows in one call. Platform applies the
AND-combined filters server-side on `GET /admin/screening-submissions`, and
`count` is the filtered total. The tool defaults to `generation=all`, because
the row being looked for often predates the active benchmark, and to
`detail=identity`, which returns only `agent_id`, name, version, status,
`submitted_at`, and `artifact_sha256`. Paging `list_screening_submissions` and
filtering client-side is the pattern it replaces: finding one name that way
once took eight 200-row pages and about 1.4 MB of JSON.

## The review queue

`get_screening_review_queue` is the operator queue: unresolved `ath_reviews`
rows, oldest hold first. It is **not** `list_screening_quarantines` — active
quarantines are auto-resolved by the platform within milliseconds and that list
is effectively always empty, which is what made the queue look empty when this
tool pointed at it.

Two platform parameters are deliberately not exposed. `status`, because a queue
is unresolved work by definition. `generation`, because it filters on whether
the held agent has a score at a benchmark version — its `active` default hides
an upload-time copy hold and any hold that outlived a rollout, both of which are
still waiting. Both are pinned in `admin.service.ts`.

Read `agent_status` before acting on a row. A `pending` review whose agent is
not in `ath_pending_review` is a hold stranded by some other path, and
`resolve_ath_review` answers 409 for it; `apps/platform/docs/ath-review-queue.md`
lists the paths that produce it.

## Hotkey-level upload bans

An ATH rejection bans one agent UUID. The separate `banned_hotkeys` gate is a
rare miner-wide control that refuses every future upload from one hotkey. Use
`list_hotkey_bans` to enumerate the active rows and retain the exact `hotkey`,
stored `reason`, and `banned_at` timestamp.

`unban_hotkey` requires that timestamp as `expectedBannedAt`, a specific
operator reason, and the exact confirmation `UNBAN HOTKEY <hotkey>`. Platform
locks and rechecks the active row, removes only that upload gate, and appends
the signed-in operator identity plus the previous ban evidence to
`hotkey_ban_audit`. Existing agent UUID statuses are never changed. The tool
returns the post-write state so a successful call proves `banned=false` and
surfaces the new audit entry.

## Reading miner source

Three tools, used in this order. Skipping the middle one is the expensive
mistake:

1. `list_screening_source_files` — the manifest: which paths exist and which
   are opaque blobs the text reader cannot show.
2. `search_screening_source` — **where**. One regex or literal scan of the whole
   artifact returning `{path, line, text}`. A miner `baseline.rs` routinely runs
   past 10,000 lines, so without this, locating the `protocol::RunResponse`
   construction that a `deferred_source_review` turns on means bisecting with
   400-line windows: six to eight reads per agent, repeatedly.
3. `read_screening_source_file` — the excerpt, once you have a line number.

Both reads page under the rules above: the search `truncated` is the scan
hitting its own match cap, and the manifest returns whole by default so no path
hides behind an offset. `opaque_skipped` counts the members no search can
reach — a `.onnx` or `.bin` weights file is never searched, and a search that
never opened one cannot clear it.
