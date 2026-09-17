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
