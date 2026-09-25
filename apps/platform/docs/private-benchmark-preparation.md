# Private benchmark preparation (not enabled)

Private inference must not run in a lease transaction or the 60-second public
generator request. The preparation ledger reserves a cryptographic salt once for
an immutable `(scope, version, seed, run size, transformation profile)` identity.
Concurrent requests reuse that row. It is not an issuance or qualification gate.

Workers commit a `SKIP LOCKED` claim before inference. Claims bind an opaque token,
profile, reserved entropy and three-hour deadline. A crashed claim can be taken
over at most three times total, with the same entropy and a new token. An expired
or superseded worker cannot pin its candidate. A semantic/provider/output failure
is terminal and visible; it is not automatically billed again indefinitely.

Completion atomically pins the validated artifact and marks preparation ready.
A transaction rollback leaves neither a ready row nor half-published artifact.
After an uncertain commit, read the immutable dataset/queue status; never infer
success from subprocess exit alone. Existing pinned bytes win over new candidates.

Queue identity, entropy and completed results cannot be changed. History cannot
be deleted through ordinary DML, and downgrade refuses a populated ledger.
Failure codes are an allowlist; provider payloads and secrets do not belong in
that field or application logs. Salt and artifact bytes stay out of dataclass
representations. SQL parameter logging is disabled and private insert failures
are sanitized. PostgreSQL/server-log access still belongs in the deployment
privacy audit.

This layer contains the queue and real-PostgreSQL fencing tests only. Worker
deployment, caller integration, bounded enqueue admission, Backroom readiness,
qualification and safe closure/reveal are separate work. Nothing here turns on
private V13 generation, issues a canary, or activates a benchmark.
# Trusted producer worker

## Lease preparation

New V13 normal, confirmation and canary pins resolve through the private store,
not public generation. Set `DITTO_PRIVATE_DATASET_PROFILE_SHA256` only to the
reviewed producer profile. Unset configuration fails closed for new V13 work;
older versions retain their existing generator. Existing tickets are not
re-pinned. Agent-level screening base pins remain distinct from the actual
validator-specific private execution pin.

Missing private artifacts reserve preparation in an independent short transaction
and return 503 with a retry interval. The attempted lease rolls back. A separate
two-connection pool avoids waiting for a connection held by the calling lease.
`DITTO_PRIVATE_DATASET_MAX_PENDING` and `DITTO_PRIVATE_DATASET_MAX_DAILY` both
default to eight; admission is serialized across profiles and counts failures
against the rolling-day limit. Limits are dataset counts, not dollar guarantees.
Equal seed/run/profile inputs share the `bench-v13` scope across CRN participants.
Terminal failed preparations require operator review, not a new salt on retry.

This configuration is not evidence of qualification and must not be enabled for
rollout before the separate qualification and disclosure-closure gates land.

`python -m ditto.private_benchmark_worker` prepares at most one queued artifact,
then exits. It is not started by API boot. It checks the approved native binary
SHA-256 and its `-profile-sha` output before claiming, then commits the claim
before invoking inference. Completion and the immutable dataset pin commit in
one fenced transaction. Cancellation leaves the claim recoverable; a known
producer rejection is terminal and is not automatically retried.

The separate worker environment requires `DITTO_PRIVATE_PRODUCER_EXECUTABLE`,
`EXECUTABLE_SHA256`, `PROFILE_SHA256`, `WORK_ROOT`, `REWRITE_MODEL`,
`REWRITE_PROVIDER`, `VALIDATOR_MODEL`, and `VALIDATOR_PROVIDER` (all with the
same `DITTO_PRIVATE_PRODUCER_` prefix), plus `OPENROUTER_API_KEY` and ordinary
Platform database configuration. Optional prefixed `REWRITE_REASONING`,
`VALIDATOR_REASONING`, and `CONCURRENCY` bind explicit producer settings.
No database/admin environment is inherited by the child. Profile inspection
receives no provider credential. Child stdout/stderr are not forwarded.

`DITTO_PRIVATE_PRODUCER_MAX_COST_USD` is also required and bounds one producer
invocation. Allocate it from the remaining approved total, including failed
attempts. The producer persists reservations and settled costs in private
`spend.json`; unknown charges retain their full reservation. See
`research/dittobench-datagen/docs/private-producer-budget.md` for supported routes
and the account-wide quota boundary. Queue dataset counts are not dollar caps.

Deployment must separately provide a dedicated non-root UID, an owner-only
0700 absolute work root with no symlink components, immutable approved binary,
network restrictions, memory/CPU and disk quotas, private diagnostic retention,
and bounded queue admission/billing. The worker itself has a two-hour deadline,
kills its producer process group, and validates owner-only bounded regular
output files. It retains private files for diagnosis; do not publish them as CI
artifacts or expose them to miners. This layer is not production provisioning,
qualification, lease authorization, or activation.
