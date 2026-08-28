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

## Provider-routed screening jobs

Build, runtime smoke, and source review have independent revisioned provider
lists. Targon is enabled for a lane only when that list starts with `targon`.
Any other list, including `['gcp', 'targon']`, is the GCE-only cutover: queued
Targon work is terminalized and GCE workers remain the authority. A remote
build is attempt-bound and becomes consumable only after Platform verifies the
complete image archive. When runtime starts with Targon, the trusted controller
promotes that exact archive to a private ephemeral registry, launches it
directly as a Rental, and records digest/workload provenance. When runtime smoke records `succeeded`, that Targon `/health` result is the
mechanical admission. Platform copies the verified Kaniko archive to the
screened-image key, creates the Targon rentals, and records the verdict.
There is no screener sr25519, no GCE worker, and no capacity-controller host. Isolated fake-gateway oracle is
skipped until a screener-to-rental prompt tool exists.

Source review is also attempt-bound. A pinned trusted worker may return a
bounded L1 observation. Certified low-risk clearance is a pass without local
L2. `require` mode uses the remote observation as-is (elevated findings
quarantine). `prefer` mode still falls back to GCE L2/L3 for uncertified
results. Platform also queues source review when runtime smoke succeeds so the
lane does not depend on the GCE worker staying alive. Job tokens are
stored only as hashes and revoked at terminal completion; provider Rental
identities and cleanup failures remain durable operator evidence.
Cloud Run may execute the same contract through a private warm service with
concurrency one. Each invocation still receives a fresh attempt-bound job token
and short-lived bootstrap token; no model key or Platform capability persists
between claims.

## Quarantine management

A current worker can return a signed, attempt-bound `quarantine` outcome with
only bounded reason and evidence digests. The platform completes that exact
lease, moves the submission to the non-scoreable `quarantined` state, and
appends a `screening_quarantines` row. Raw source, model transcripts, private
prompts, and challenge contents are never stored in the platform database.

Backroom and other operator clients use the bearer-protected endpoints below:

- `GET /api/v1/admin/screening-quarantines`
- `GET /api/v1/admin/screening-quarantines/{quarantine_id}`
- `POST /api/v1/admin/screening-quarantines/{quarantine_id}/resolve`
- `GET /api/v1/admin/screening-submissions/{agent_id}` returns the exact
  submission metadata and complete screening-attempt history for an agent UUID.
  It does not return source, artifact URLs, or artifact contents; those remain
  behind the separately audited artifact endpoints.

Resolution actions are append-only in `resolution_history`. A resolved rejection may
be corrected to `release` while the agent is still rejected; other second resolutions
remain conflicts. This narrow correction path preserves the original actor, reason,
and timestamp while allowing a reviewed false positive to resume evaluation.

Resolution requires `X-Admin-Actor` and one of `release`, `rescreen`, or
`reject`. A row lock makes resolution single-writer. Release pins a dataset if
needed and promotes to evaluation; rescreen returns the preserved submission to
the screener queue; reject retains the submission and prior scores but prevents
evaluation until a future policy-version rescreen.

Quarantine listings default to `sort=oldest` so operator queues process the
longest-waiting submission first. Clients may request `sort=newest`; pagination
uses the same timestamp and quarantine-ID direction for deterministic results.

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
