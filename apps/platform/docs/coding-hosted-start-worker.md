# PostgreSQL-backed hosted start handoff

Status: concrete Platform start-store and local Go/Python handoff, tested against
migrated PostgreSQL. No public endpoint, scheduler, deployment or private coding
execution is enabled. This is not a completed private shadow evaluation.

## Authority and commit

`HostedStartStore` fixes its worker UUID from trusted process configuration. Each
request reconstructs the complete immutable assignment and verifies its canonical
digest, admission, deadline, artifact and screened-image identity. It also checks
the current screening policy, registered release lifecycle, bound private selection
and unfrozen/unclosed task phase. Lock order is release, agent, assignment, task.

It then calls the existing `start_hosted_attempt` ledger function. The enclosing
transaction must successfully exit before `newly_started=true` is returned.
Concurrent calls yield at most one fresh permission. Same-worker replay yields
false; another worker, changed authority, retirement or closed phase fails closed.
Rollback permits a later first start, while lost acknowledgement after commit
never permits a second launch. The existing database guards and schema are reused;
there is no migration, alternative ticket table or v1 ticket conversion.

The private handoff schema is `dittobench-coding-hosted-start-v2`. It carries only
opaque execution identity and screened-image metadata, not task content, catalog
indexes, object grants, image URLs, inference credentials or key material. Its
profile capability is deterministically `hosted-<attempt UUID>`; it is a scope
identifier, not a bearer. Deadlines use the assignment's exact integer Unix time.
The response binds the canonical request SHA-256 and an explicit Boolean result.
Unknown fields remain non-authoritative; missing/malformed known fields fail.

## Local process boundary

`codingharness.HostedStartCommand` implements the native lifecycle's start-store
interface. The approved installed Platform virtualenv interpreter runs:

```text
<absolute-python> -I -m ditto.coding_hosted_start_worker --worker-id <worker UUID>
```

This is a direct process invocation, not a shell or public HTTP API. The owner must
approve and protect that interpreter and installed Platform code as part of the
execution profile. Go supplies only explicitly configured `POSTGRES_*` variables;
ambient environment, `PYTHONPATH`, dynamic-library overrides and provider/storage
credentials are not inherited. These database values remain inside the trusted
Platform boundary and must never enter a miner/validator container or a public
receipt. The factory deep-copies and redacts its credential-bearing configuration.

Stdin is capped at 16 KiB, stdout at 1 KiB, and the whole call at the smaller of
30 seconds and the assignment deadline. The database transaction has a 20-second
timeout. Stderr and exception details are not returned. The client makes one
attempt, verifies the bounded duplicate-free result and request digest, and
rejects late, malformed or failed-process responses. The helper returns success
only after commit; exit/pipe loss is ambiguous, never permission to rerun.

## Verification and remaining work

The Platform tests invoke the real Go lifecycle, real installed Python helper and
real migrated PostgreSQL. Only the candidate container runtime is a synthetic
test double. Other tests cover concurrent start, replay with a new instance ID,
commit failure/rollback, cancellation while waiting for a row lock, retired or
closed authority, image/assignment drift, environment confinement and output
limits. This does not prove live Docker isolation, Hippius access or key custody.

Next integration must assemble the approved screened-image transport and verified
private projections, provide native inference issuance/revocation, run the worker,
freeze after quiescence, grade pristinely and finalize sealed signed evidence.
Host-crash reconciliation remains required; neither this helper nor registration
alone launches a worker or reopens a previously started assignment. Public
practice, embeddings, leaderboard scores, weights and rewards are unchanged.
