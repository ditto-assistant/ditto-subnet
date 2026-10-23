# Screening protocol dependency

The API owns queue state, leases, verdict acceptance, screening history, and
public status projection. The public `ditto-screener` repository owns the
build/run worker. They share only `ditto-screening-protocol`, pinned in
`pyproject.toml` and `uv.lock` to an exact public-repository commit.

The protocol package contains request/response models, `AgentStatus`, artifact
metadata, `SCREENING_POLICY_VERSION`, and the canonical signing function. The
API never imports worker application code.

Policy 9 adds the screener-built image handoff. A passing worker initiates an
attempt-bound multipart upload to a unique immutable key. After completion the
platform streams every final byte to verify the declared full-archive SHA-256
and size; multipart ETags and per-part checksums are not treated as equivalent.
The upload identity and image metadata are bound into the canonical v5 verdict
signature. Accepted objects cannot be replaced through an old part URL, and
validators receive short-lived URLs for both the source and screened image.
Legacy agents without image metadata remain scoreable through the source-build
fallback.

Run `scripts/cleanup_screened_images.py` daily. It aborts incomplete multipart
uploads and removes completed-but-unaccepted objects after one day. Accepted
images are retained while evaluating and for each miner's current best eligible
scored agent; non-champion images older than 30 days are detached first (which
restores source-build fallback) and then deleted. Infrastructure lifecycle rules
may abort stale multipart uploads and expire noncurrent versions, but must not
apply a blanket age expiry to current objects needed for rescoring.

Roll out the backward-compatible validator scorer and subnet worker first, then
the platform migration and policy-9 pin, and finally the policy-9 screener. The
old screener halts safely when the platform requires the new policy; old
evaluating records continue through the build fallback.

Policy 10 retains that image contract and versions the strict source-review
court. Every Luna and escalated L2/SOL finding carries one signed decision for
I1-I7; I4 derived authority, I5 production generality, and I7 model tool
planning remain independent of the historical two limbs. Deploy the shared
protocol and Platform requirement, reissue the protected policy manifest with
version 10, then deploy policy-10 workers. Existing policy-9
scores and findings are historical evidence, not silently migrated verdicts;
only new or explicitly rescreened attempts attest policy 10. A policy bump
re-queues only submissions admitted to the active benchmark era; a historical
submission the validator allocator no longer leases is never rescreened for a
bump alone and projects as `not_queued`, keeping the `waiting_screening`
backlog the capacity controller scales on honest.

Policy 13 preserves the historical seven-invariant payload as nested assessment
schema version 1 and adds I8 evaluation independence through schema version 2.
Workers carrying v13 still serve policy v10-v12 during a scheduled transition;
their exact-version tool schemas require seven decisions for old policies and
eight for v13. Shipping the built-in version does not activate it. The separate
`SCREENING_ACTIVATION_CEILING_POLICY_VERSION` is the highest version the
scheduling API presents as activation-ready, so fleet adoption alone can never
be mistaken for readiness. It stayed at v12 while v13 code was distributed
(#1801) and moved to v13 on 2026-09-14, after the strict two-outcome contract
shipped and both production screeners reported builtin policy 13 on release
0.264.0. Raising the ceiling only makes v13 schedulable: the queue still
requires the floor until an operator schedules an activation window and
`activate_at` passes. Move the ceiling again only after the readiness,
retry/deadline, transition, opaque-component verification, and exact-artifact
emission rules in `workers/screener/docs/policy-v13.md` are satisfied for the
next version.

## Provider-routed screening

Enrolled Hetzner workers claim attempts and return signed terminal verdicts.
The GCE managed instance group is a bounded outage and backlog fallback.
Provider routing and per-node concurrency are revisioned and audited through
Backroom. New routing writes cannot select the retired provider; historical
provider values remain readable in existing audit rows. The separate node-job
APIs can settle previously queued build, runtime, and source-review rows, but
they do not open new screening attempts.

The signed screener worker remains the terminal verdict writer. Platform
verifies source and image identities and records each job result without
turning a source-review hold into an automatic reject.

## Quarantine management

A current worker can return a signed, attempt-bound `quarantine` outcome with
only bounded reason and evidence digests. The platform completes that exact
lease, moves the submission to the non-scoreable `quarantined` state, and
appends a `screening_quarantines` row. Raw source, model transcripts, private
prompts, and challenge contents are never stored in the platform database.

### Inconclusive screenings are held for operator review

An `inconclusive` outcome reaches Platform from two different causes, and
`ditto/api_server/screening_inconclusive.py` tells them apart by reason code:

- **A reviewer non-verdict** (`l2-model-inconclusive`,
  `source-review-inconclusive`, `l3-adjudicator-inconclusive`, a critic or
  adjudicator disagreement, an unresolved violation lead, a review budget that
  stopped on its notes ledger, or no code at all). The source review ran and
  could not decide, so retrying the same artifact under the same policy has no
  reason to conclude differently. Platform completes the attempt as
  `quarantined`, moves the submission to `quarantined` with the public reason
  "Screening was inconclusive; held for operator review", and opens an
  **active** `screening_quarantines` row. Nothing resolves that row
  automatically: the infrastructure retry planner only considers
  `screening_failed` rows, the claim never takes a `quarantined` agent, and
  the attempt no longer counts toward the repeated-inconclusive park cap. The
  operator releases, rejects, or rescreens it with `resolve_screening_quarantine`
  or the guarded preview/execute batch, exactly like an anti-cheat hold. It is
  counted as `escalation` in the ordinary source-review SLO.
- **An infrastructure fault carried as `inconclusive`.** The worker reports
  an L2/L3 provider or transport failure as `inconclusive` once an earlier
  stage recorded a finding, and the signed-runtime preflight reports
  `l2-runtime-evidence-unavailable` that way. A code registered as
  `fleet_infra`, `provider_backoff`, or `operator_retry` in
  `ditto_screening_protocol.reason_codes`, or one carrying a worker fault
  marker (`provider-fault`, `response-contract`, `credits-exhausted`,
  `transport-failure`, `analyzer-contract`, `analyzer-exited`,
  `http-<status>`, or a timeout or exception-class tail), keeps the previous
  behavior: the attempt ends `expired`, the submission parks in
  `screening_failed` with "Screening was inconclusive; manual retry required",
  and an operator authorizes one exact retry with `retry_failed_screening_now`.

The split is a deny list of fault shapes because the worker composes most
codes dynamically; an unrecognized code is held for a human rather than
retried. Two cases keep the retry path even for a reviewer non-verdict: a
build-only attempt (it ran no source review), and a released scored policy
canary, which keeps its board position and pauses its one-row rollout for an
operator retry because the quarantine court only resolves a `quarantined`
agent. A deferred deep review is unchanged: it keeps its
`ath_pending_review` hold.

Submissions that were parked in `screening_failed` as inconclusive before this
routing shipped are not migrated. An operator can rescreen one with
`retry_failed_screening_now`.

Backroom and other operator clients use the bearer-protected endpoints below:

- `GET /api/v1/admin/screening-quarantines`
- `GET /api/v1/admin/screening-quarantines/{quarantine_id}`
- `POST /api/v1/admin/screening-quarantines/{quarantine_id}/resolve`
- `GET /api/v1/admin/screening-submissions/{agent_id}` returns the exact
  submission metadata and complete screening-attempt history for an agent UUID.
  It does not return source, artifact URLs, or artifact contents; those remain
  behind the separately audited artifact endpoints.
- `GET /api/v1/admin/screening-submissions/{agent_id}/attempts/{attempt_id}/failure-diagnostic`
  returns the bounded, sanitized private failure detail retained for one exact
  attempt. Public submission history omits it; Backroom exposes it only through
  the separately authorized artifact-read scope and supplies the operator actor.

Resolution actions are append-only in `resolution_history`. A resolved rejection may
be corrected to `release` while the agent is still rejected; other second resolutions
remain conflicts. This narrow correction path preserves the original actor, reason,
and timestamp while allowing a reviewed false positive to resume evaluation.

Resolution requires `X-Admin-Actor` and one of `release`, `rescreen`, or
`reject`. A row lock makes resolution single-writer. Release pins a dataset if
needed and promotes to evaluation; rescreen returns the preserved submission to
the screener queue; reject retains the submission and prior scores but prevents
evaluation until a future policy-version rescreen.

Every quarantine carries two codes from disjoint vocabularies, and they are
never interchangeable:

- `screening_reason_code` is why the screener held the submission: the code
  from the signed verdict that opened the quarantine. It survives the
  resolution, so a resolved quarantine still reports the lead the operator
  ruled on, and the append-only `screening_review_events` ledger keeps that
  same code verbatim on the manual event it snapshots. It is screening-origin
  provenance, not a decision — `behavioral-oracle-passed`, for instance, is
  emitted with a CLEAR disposition by the screener, so reading it as the reason
  for a later rejection inverts its meaning.
- `resolution_reason_code` is the operator's own ruling, derived from
  `resolution` as `operator-released-quarantine`,
  `operator-rescreened-quarantine`, or `operator-rejected-quarantine`. It is
  null while the quarantine is active and null on an automated review event,
  because an automated rejection is the screener's own verdict arriving over
  the signed screening path, not an operator ruling.

The quarantine, review-event, and miner-summary responses also carry a
deprecated `reason_code` alias holding exactly the same screening-origin code
as `screening_reason_code`. Platform and Backroom deploy in parallel from one
release with no ordering between them, so a Backroom that has not been
redeployed still requires the old name and would reject every quarantine item
without it. The alias is never a second fact: Backroom coalesces it onto
`screening_reason_code` and drops it, and both the alias and that fallback are
removed once no supported Backroom reads the old name.

Deriving the ruling code rather than storing it keeps rows written before the
field existed correct without rewriting an append-only ledger, and leaves no
denormalized copy to drift. The vocabularies are disjoint — no screening-origin
code begins with `operator-` — so a code on its own still says which of the two
facts it records. They are also deliberately distinct from the
`operator-rejected-screening` code minted by the pre-quarantine
`/api/v1/admin/screening-submissions/{agent_id}/reject` route, whose retry guard
treats that exact token as proof it already ran.

A manual resolution stamps the matching ruling code onto the agent, so the
miner-facing `screening_reason` / `screening_reason_code` pair returned by
`GET /api/v1/retrieval/agent/{agent_id}/status` and
`GET /api/v1/retrieval/agent-by-hotkey` always describes a single decision
rather than pairing the operator's prose with a stale screening code. The
pre-quarantine retry routes clear `screening_reason_code` for the same reason:
the submission is back in the screener's hands, so no verdict describes it and
the operator's prose stands alone until the next attempt concludes. That clear
loses nothing, because the attempt row keeps the earlier lead verbatim.

Quarantine listings default to `sort=oldest` so operator queues process the
longest-waiting submission first. Clients may request `sort=newest`; pagination
uses the same timestamp and quarantine-ID direction for deterministic results.

## Lineage-scoped review

When a claimed attempt is an ordinary source review (not build-only, not an
exact duplicate, not a pinned canary or forced full review) and the same
`miner_hotkey` has an earlier `version` of the same agent `name`, the claim
stores that earlier agent as `screening_attempts.lineage_parent_agent_id` and
emits it on the queue item as `lineage_parent_agent_id`.

The worker then calls `POST /api/v1/screener/agent/{agent_id}/review-lineage
?attempt_id=...` under the same lease checks as the artifact download. The
response is a digest-sealed `ReviewLineagePacket` (protocol
`ditto_screening_protocol.review_lineage`): parent and history, every
quarantine resolution, ATH action (with batch evidence references) and resolved
dispute on those versions, the parent's automated review outcomes, and the
bounded per-file diff. The first fetch binds `lineage_scope` and
`lineage_packet_sha256` to the attempt; a later fetch that would produce a
different packet is refused with 409. Any fetch failure leaves the worker on an
ordinary full review.

`scope` is `full` when the Dockerfile, a build manifest or an entrypoint
changed, the diff exceeds 2,500 changed lines or 64 files, an opaque or unread
file changed (compared by member digest), no ruling or finding exists under the
attempt's policy, or the lineage has no review history. The L1 opening message
and the L2/L3 dossier (`review_lineage` section) carry the packet, and the L2
result and analyst caches include its digest, so an ordinary review's caches are
unchanged.

## Miner disputes

A miner may dispute a resolved quarantine rejection exactly once per submission.
The request is accepted only while the submission remains rejected and only when
its sr25519 signature verifies against the hotkey recorded at upload. The miner
signs the following canonical UTF-8 payload, where `message` is trimmed before
hashing:

```text
ditto-dispute-v1:{agent_id}:{sha256(message)}
```

The submission dashboard generates that payload and a ready-to-run command after
the miner enters the local wallet and hotkey names:

```bash
btcli wallet sign --wallet-name '<wallet-name>' --wallet-hotkey '<hotkey-name>' \
  --use-hotkey --message 'ditto-dispute-v1:<agent_id>:<sha256>' --json-output
```

`--use-hotkey` prevents an accidental coldkey signature. The miner pastes the
128-character `signed_message` value from the command output into the dispute
form. Wallet and hotkey names are used only to construct the command in the
browser and are not included in the dispute request.

`POST /api/v1/public/agent/{agent_id}/dispute` accepts a 20–1000 character
message and a 128-character hexadecimal signature. Database uniqueness on both
`agent_id` and `quarantine_id` enforces the one-dispute limit under concurrent
requests. The public submission pipeline exposes only dispute status, timestamps,
and the final `release` or `uphold` result; the miner's message remains private.

Operators use the same admin bearer-token boundary as quarantine review:

- `GET /api/v1/admin/screening-disputes`
- `POST /api/v1/admin/screening-disputes/{dispute_id}/resolve`

Resolution requires `X-Admin-Actor`. `release` atomically records the accepted
dispute, changes the effective quarantine resolution to release, and returns the
submission to evaluation. `uphold` records a final review while leaving the
submission rejected. The original rejection and its operator reason remain in
append-only quarantine history in either case.
