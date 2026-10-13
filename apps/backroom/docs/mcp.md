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

`get_validator_capacity` reads `GET /api/v1/admin/validator-capacity`, a
bounded roll-up of the same heartbeat, slot-policy, and lease reconciliation
`get_validator_fleet` serves, limited to validators inside its online window.
It reports serviceable against claimed ordinary slots, each live lease's age and
check progress, the oldest active-era submission that passes the fleet-wide
queue filter and holds no lease, and live chat and embedding load against the
global concurrency limit. `checks_per_minute` and
`estimated_remaining_slot_minutes` are estimates measured from ticket issue to
the latest heartbeat, and they are null, never zero, until a check completes. The
tool is read-only and schedules nothing.

`list_validator_assignments` reads live leases from
`GET /api/v1/admin/validator-assignments`. Each lease carries `seed`, the exact
decimal dataset seed the validator runs, as a string. A JSON number would round
a 64-bit seed above 2^53, so two different seeds could compare equal. Two
`continual_retest` leases on one agent with equal `seed` values are the same
paired shared-seed run. That lets an operator confirm two simultaneous runs
match before either is accepted, without copying the seed into a manual run.
`seed` is null for a ticket with no seed yet, and for a Platform that predates
the field. It is a seed id only. Dataset contents never leave Platform, and the
confirmation plan's pending seeds stay server-side
(`get_continual_retest_diagnostic` reports only `pending_seed_count`).

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

`get_claim_provenance_cases` explains the bench v13+ claim-provenance
aggregate on one accepted score, case by case, through
`GET /api/v1/admin/agents/{agent_id}/claim-provenance`. Every key is exact: the
agent, its artifact SHA-256 (a different artifact returns 409), and the run id
from `get_agent_scores`. `caseId` narrows the read to one case and `finding` to
one closed-vocabulary gate. The default set is the stored flagged set, so
`matched_cases` equals the public `flagged_case_count`. Each case returns the
persisted `claim_provenance` record (verdicts and counts), the catalog record
with per-completion relay metadata (digests, no text), the relation, twin group
and cost factor, the scorer's own notes (a note that quotes a case value, such
as a forbidden argument or bait tool, is withheld), and gate notes whose
`note_id` is what an owner dispute cites. `not_persisted` names the fields the
scorer computes but does not store: the credited response field, the per-token
claim comparison, completion ids and the normalization trace. Their absence is not
evidence either way. The tool never returns the answer key, prompts, user
records, tool results or completion text.

`get_outlier_escalation_dry_run` replays the same escalation decision through
`GET /api/v1/admin/outlier-escalation/dry-run` over the current scored ledger
for one benchmark version, active by default. It uses the ledger that scoring
reads at finalization, one scored row per owner, and judges each row against
all the others. The mode is ignored. Settings are the effective ones, and
`minCohortSize`, `modifiedZThreshold` and `minCompositeFloor` can override them
for the replay only. It returns the cohort size, ledger median and MAD, the
exact would-trigger count, and up to 20 rows (100 at most) with each agent's
composite, modified z-score and evidence. Held agents are not replayed. Each
row is judged against today's ledger, not the ledger at its own finalization.
The tool opens no hold and changes no setting.

`get_continual_retest_diagnostic` explains, for one exact agent UUID, what
`get_leaderboard` and `get_agent_scores` cannot: why a scored generation is or
is not its owner's emission representative, and why it is or is not earning
shared-seed retests. It reads
`GET /api/v1/admin/agents/{agent_id}/continual-retest-diagnostic` with
`backroom:read`. The response carries the canonical and official continual
composites with their sample counts and completed-wave depth, the same-owner
family with the representative, its margin and the `owner_family_key` term that
selected it (`representative_selection`), membership in the raw wave, folded
emission set and resolved retest cohort, and the cohort and emission cutoffs as
`{composite, gap, tie_band, within_tie_band}`. `admission_reason` is the
exclusion reason. A negative `cohort_cutoff.gap` on an agent still outside the
cohort is owner suppression, not a score it failed to reach;
`same_owner_challenger` is the bounded catch-up admission, not a second
emission slot. `claim` runs the issuance lane's own gates for an empty validator
hotkey, so no lease, event or slot is attributed. It gives the scheduled round,
catch-up set, spare-capacity window, idle-retest gate, route priority and
`decision`, the first gate a polling validator would hit now. A
`chain_unavailable` decision means the block read failed, not that the lane is
idle. The tool also returns ticket counts and the latest ticket and accepted
result. Outstanding work is only a count, `pending_seed_count`. Pending seed
values, confirmation datasets, prompts and answer keys never leave Platform, and
unknown upstream fields are stripped. The raw and folded seed IDs it returns
are already-scored confirmation seeds, as exact decimal strings. The snapshot
grants no work and changes nothing.

`https://backroom.dittobench.ai/mcp` is an OAuth-protected Streamable HTTP MCP
server exposing the same operations as the console: screening quarantines and
disputes, validator queue/slot/inference policy, the scoring lease TTL,
benchmark rollouts, scoring policy, scores and leaderboards, and the emission
burn.

`get_scoring_lease_settings` / `set_scoring_lease_settings` (#1156) read and
revise the deadline stamped on new canonical and score-retest replacement
tickets (default 180 minutes, bounded 60-240 so it stays inside the validator
245-minute restart drain). A revision needs `expectedRevision`, a reason, and
`APPLY SCORING TICKET TTL <n> MINUTES`; live tickets keep their minted deadline.

It was ported from the private `ditto-assistant/backroom` repository, which
keeps only `backroom.heyditto.ai` and the Ditto app surface. Feature flags and
app reviews are deliberately **not** served here: they reach the private product
API this deployment holds no credentials for. `src/lib/subnet-surface.test.ts`
enforces both halves of that boundary — the MCP must be wired, those tools must
not be.

## Authorization

`get_treasury_ledger_readiness` includes bounded `chain_reads` diagnostics for
completed epoch-schedule and requester-activation reads in this Platform
process. Counts reset on process restart and include background schedule reads;
they are not an endpoint availability metric. The last failure retains its
request ID, elapsed time, fixed stage, step and category after a later success.
Provider messages and URLs are omitted. These observations grant no weight or
spending authority and do not replace fresh chain permission checks.

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
| `backroom:artifact:read` | Miner-submitted source and sensitive diagnostics: tarball URLs, file listings, source search, copy/lineage/baseline diffs, and sanitized private screening failure details. |
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
The unauthenticated `/mcp` 401 challenge advertises all three ordinary scopes, because
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

### Dedicated treasury receipt observer

An observer must explicitly request **only** `backroom:treasury:observe`. This
scope is excluded from both OAuth discovery metadata and the ordinary 401
scope bundle: general MCP clients may request every advertised scope, while
mixing observer access with read, source or write is refused. All root and
`/mcp` authorization-server and protected-resource metadata advertise only
the three ordinary scopes. The staff member
must explicitly select receipt observation and remain write-level at consent,
code exchange, refresh and each MCP request. The original seven-day staff
session and token TTL limits still apply; blocking, removing write entitlement
or revoking the grant prevents further observation. No grant is issued by
deploying this code.

The dedicated server exposes exactly two tools, without the general read floor:

- `get_treasury_settings` **requires** an exact positive `revision`. It returns
  a singleton `history` containing the immutable revision, checksum and raw
  settings JSON (at most 64 KiB), including future fields needed to verify the
  checksum. It omits actor, reason and unrelated history. Corrupt or oversized
  history is refused, never replaced by default settings. The ordinary read
  tool keeps its existing current/history response when revision is omitted.
- `record_treasury_receipt` verifies one finalized selection, requires the exact
  receipt confirmation, and attributes it to the authorizing staff email.
  Platform's chain, policy, replay and append-only checks are unchanged. Vendor
  payment is not proof of provider credits or authority to spend.

Only stateless POST initialize/initialized/ping/tools-list and calls to those
two tools are permitted. Direct requests for other tools, resource reads,
downloads, JSON-RPC batches and bodies over 64 KiB fail before Platform access.
The observer performs fresh tool discovery and refuses a broader catalog; its
local allowlist alone is not credential custody protection. An operator must
separately approve and obtain the dedicated grant through ordinary browser
consent. No desktop credential copying, mint/refresh helper, secret provisioning
or observer activation is included.

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

Keep input schemas portable. A client that cannot parse one tool's JSON
Schema drops that tool from its imported catalog without an error, even after
a fresh connect, while the server's `tools/list` and `get_backroom_tool_help`
still show it. Do not use `z.tuple`: it serializes to draft-07 array-form
`items`, which is what kept `activate_v13_scorer_cohort` and
`rotate_v13_scorer_cohort` out of refreshed Codex catalogs (#2490, #2559). Use
a bounded array such as `z.array(z.string()).length(3)` instead. The catalog
test fails on any array-form `items`, `prefixItems`, or `additionalItems`.
The catalog itself does not depend on scope: a read-only connection lists the
write tools too, and the scope check refuses the call.

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

## Report-only L2 canary preflight

`get_l2_report_canary_preflight` runs the same exact-source guard predicate that
`schedule_l2_report_canary` enforces (and that claim and completion recheck),
without a lock, queue write, or storage read. Pass the values you intend to
schedule with (`artifactSha256`, `expectedAgentStatus`, `expectedScoreCount`,
and for a legacy null-SHA attempt `historicalRulingKind` / `historicalRulingId`).
Each guard returns `passed`, `current`, `expected`, and the exact 409
`conflict_detail` the scheduler answers when it is the first failure; an omitted
expected value returns `passed=null` rather than a guess.

The preflight is advisory and non-authorizing (`authority: "none"`): it grants
nothing, and scheduling reruns the same predicate under row locks. The first
guard that is not known to pass is where scheduling would refuse, with that
guard's detail. With a historical ruling, scheduling checks the ruling first:
`ath_clear_action` (`ATH clear action missing`), `historical_ruling`
(`historical ruling changed`), then `source_object_verified`
(`current source object differs from ruling`, or 503 when storage is
unavailable). Only then does it check the exact-source guards and the arrival
benchmark. An unavailable arrival benchmark version returns
`arrival_bench_version: null` and a failed guard, which scheduling also refuses.

The score guard compares the raw `Score` row count for the agent, not a filtered
or accepted-score view. A legacy attempt with a null pinned artifact SHA always
fails `attempt_artifact_sha256` without a historical ruling, and a ruling cannot
schedule an attempt that has a pinned SHA. `source_object_verified` stays
`null` because only scheduling re-hashes the stored object. The response also
shows any active queued or leased canary for the attempt and whether the
report-only scored-runtime packet a claim needs is currently available; a
queued canary waits without a packet. The response carries no source text,
signed URL, credential, or private challenge material.

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

## Held V13 court clears

Platform does not admit a policy-v13 source-only court `clear` on a PASS
verdict, so the screener transports it as a quarantine carrying the evidence
code `source-review-awaiting-v13-verification`; `list_screening_quarantines`
shows it with `screening_reason_code` `adjudicated-source-review-clear`.
`release_verified_v13_court_clear` is the bounded release for exactly that
hold. Platform re-verifies the retained court evidence before and again under
the row locks: policy v13, the exact attempt and artifact SHA-256, an enforced
adjudicator posture bound to the claim, a `clear` adjudication matching its
signed digest, and the screener's completion-receipt signature over both. It
then moves the submission to `evaluating` like an ordinary release and records
the verified digests on the manual review event. Anything else answers 409 with
the precise reason, and the hold stays for `resolve_screening_quarantine`.
Nothing releases automatically.

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

## Diffing two submissions

`get_copy_review_source_diff` pairs a held agent only with the reference its
copy review names, and `get_screening_baseline_diff` only with the starter kit.
To check whether a resubmission really removed a rejected mechanism, diff it
against the rejected ancestor directly with
`get_lineage_source_diff(agentId, referenceAgentId, path?)`. It works for any
two stored agents, reference -> agent, with no hold on either:

- Without `path` it returns the per-file manifest in the copy-review shape:
  `added` / `removed` / `modified` / `identical` / `renamed` (with `from_path` /
  `to_path`), line counts, `normalized_identical`, and `omitted_paths` the
  bounded read skipped (not compared, so never read as a removal).
- With `path` (taken from that manifest) it returns that one file's bounded
  unified diff instead.

One tool covers both modes to keep the catalog inside its byte budget. It needs
`backroom:artifact:read`, and the two ids must differ. Platform writes one
artifact-fetch audit row per agent read (`admin.get_lineage_source_diff` or
`admin.get_lineage_source_diff_file`, each tagged candidate or reference), so
"who read this source" answers for the ancestor too.

The source readers publish their agent ids as plain `format: uuid` rather than
zod's long uuid regex, also for the byte budget. Each service function still
parses its input with the strict uuid schema before calling Platform.
