# Native hosted inference authority

Status: private worker policy/grant/request ledger with PostgreSQL tests. No HTTP
route, provider transport, provider key, source publisher or production worker is
enabled here. This is not a live model call or a completed private benchmark.

## Identity and policy

`HostedInferenceLedger` fixes one trusted Platform worker UUID at construction.
Grant UUIDs are identifiers, not standalone bearer credentials. A future transport
must authenticate the Platform worker and mediate the existing source-bound miner
capability; these methods must never become an unauthenticated API.

One immutable grant belongs to one started assignment and private task. Issuance
checks worker/attempt/assignment, current artifact, release lifecycle, authoring
phase, native policy digest and exact approved execution-profile bytes. Profile
budgets cap the policy allowance. Expiry is the earlier of assignment expiry and
started-at plus the approved wall budget. Revoked/expired grants cannot be renewed.

The native policy locks Luna, Azure EU, medium reasoning, prompt/tool digests,
single-tool/nonstreaming behavior, no fallback, private-account/ZDR posture and
request/token/cost/time ceilings. Its schema/digest are v2. Only static Chat
Completions, prompt and tool validators are reused: no v1 ticket, grant,
settlement or retry state is involved. The initial native policy permits no retry.

## Commit before dispatch

`reserve` verifies the locked model request and policy. Ceiling estimates must
come from a trusted provider adapter, never miner-reported counts. Output ceiling
equals the request maximum. Prompt/cost estimates require a reviewed tokenizer
and pricing profile; this ledger does not implement or certify that estimator.
Until that adapter exists, there is no authorized outbound inference path.

The reservation commits before returning fresh dispatch permission, sequence,
request digest and expiry. Exactly one request may be outstanding per grant.
Same-identity replay returns no fresh permission; changing bytes/ceilings conflicts.
A lost commit acknowledgement must never cause blind dispatch or identity reroll.

| Request state | Budget charged | Consequence |
| --- | --- | --- |
| reserved | Full ceilings | Blocks another dispatch and patch freeze |
| settled | Verified actual usage | Unused reservation becomes available |
| uncertain | Full ceilings | Grant permanently revoked |

Every request consumes one count slot permanently. SQL guards serialize budget
admission and prevent identity/budget rewrites, deletion and NULL-state bypasses.
The tables contain digests/accounting, not raw model text, tasks or credentials.

## Settlement, revocation and freeze

Only a trusted adapter may settle, after verifying provider receipts, response
bytes, route/no-fallback facts and billing. Native settlements bind evaluation,
attempt, grant, request, sequence, policy and request digest. Actual usage must
fit the reservation. Receipt digests cannot be credited to another request.
Replays are idempotent. Conflicting/over-limit observations remain unresolved
and require private failure evidence and controlled abort, not guessed zero usage.

Revocation closes admission and reports whether the ledger drained. Reserved calls
may settle after revocation, expiry or retirement. `mark_uncertain`, only after
transport quiescence, retains ceilings and revokes the grant; it cannot overwrite
a settlement. `accounting` returns verified totals only after revocation with no
pending or uncertain requests. Otherwise totals are null. Zero calls after
revocation is a representable not-invoked case.

The private-task freeze trigger requires any existing grant to be revoked with
no reserved requests. Task closure also revokes its grant. Uncertainty may permit
a forensic patch freeze but never proves verified model accounting or a scoreable
result. DB state cannot prove that a provider/socket/process stopped: the worker
must revoke its source route, cancel/drain transport and preserve evidence first.

Admission lock order: release, agent, assignment, private task, grant, request.
Completion/access removal takes assignment, task, grant, request and never then
locks a release. Provider I/O must remain outside database transactions.

## Verification and next integration

Tests use migrated PostgreSQL and synthetic requests/receipts for concurrency,
replay, quotas, unused-reservation release, rollback, revocation races, duplicate
receipts, immutable rows and freeze gating. No live provider/billing is proven.

The [native provider adapter](coding-hosted-provider-v2.md) now consumes this
ledger privately; it still requires a reviewed estimator and live provider
profile. Next: authenticated source-bound relay integration, followed by full
worker execution and sealed terminal evidence. Public practice, ordinary scores,
weights and emissions are unchanged. Migration alone cannot activate coding.
