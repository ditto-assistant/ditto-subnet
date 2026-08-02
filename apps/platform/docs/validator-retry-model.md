# Validator retry & exhaustion model

How a submission gets scored, how many times a validator may re-attempt it, and
how an operator recovers a submission stranded by validator-side infrastructure.

## Scoring quorum

A submission (`agent`) is finalized once **`SCORING_QUORUM = 3`** distinct
validators each post a valid score. Each validator leases a `ValidatorTicket`
for the agent (one ticket per `(agent, bench_version, validator)`), runs the
benchmark, and either posts a score (ticket → `scored`) or lets the lease lapse.

## Per-validator attempt budget

Each validator gets a bounded number of attempts **per benchmark version**:

| Constant | Value | Meaning |
| --- | --- | --- |
| `MAX_ATTEMPTS_PER_VERSION` | `1` | Base attempts a validator may spend on one agent+version. |
| `manual_retry_grants` | `0`+ | Per-ticket operator extension; raises the cap for that ticket. |
| `infra_retry_grants` | `0`–`8` | Per-ticket automatic extension earned when a lease fails on validator-side infrastructure; raises the cap so an outage doesn't spend the agent's budget. |
| `MAX_AGENT_INFRA_RETRY_GRANTS` | `12` | The same allowance summed over **every** validator on one agent+version. Reported `infrastructure` verdicts stop earning grants once the fleet total reaches it. |
| `RETRY_COOLDOWN` | `6h` | Delay before the **same** validator may re-lease after a timeout. |

The issuance cap for a ticket is:

```
attempt_count  >=  MAX_ATTEMPTS_PER_VERSION + manual_retry_grants + infra_retry_grants
                                                                  →  no more reissue
```

`attempt_count` increments each time the expired ticket is **re-leased**
(`issue_ticket`), not when it fails. Key consequences:

- **Another validator may pick the agent up immediately** — the cap and cooldown
  are per-validator, so a timeout on one validator never blocks the other two.
- **A benchmark-version bump resets the budget.** Tickets are keyed by
  `bench_version`, so repaired scoring software revisits the artifact with a
  fresh 1-attempt budget on the new version.
- **Infrastructure failures don't consume the agent's budget.** The validator
  reports a signed `fail_job` with `reason` (`infrastructure` vs
  `scoring_error`). On `infrastructure` the platform bumps `infra_retry_grants`
  (bounded at `8`), which offsets the `attempt_count` the reissue adds — so a
  validator-side outage (e.g. a model-relay/upstream failure) never spends the
  agent's genuine `MAX_ATTEMPTS_PER_VERSION` budget. A `scoring_error` is the
  agent's own failure and consumes an attempt normally.
- **…but only up to a per-agent bound.** `infra_retry_grants` is per *ticket*,
  and a ticket is one (agent, version, validator) slot, so eight grants per
  validator across a validator pool of any size is unbounded per agent. An
  artifact whose run reliably reports a scorer-side infrastructure code
  therefore never spent its budget, never reached a verdict, and re-occupied
  quorum slots indefinitely — ditto-subnet#279, where `mnemox-v55` reached
  `attempts_used: 9` against the then-current base budget of 2 with zero scores
  while the family held three validator slots per round for a day. A **reported**
  `infrastructure` verdict now additionally checks the fleet-wide total for
  that agent and version against `MAX_AGENT_INFRA_RETRY_GRANTS` (`12`). Past it
  the grant is refused, the attempt is billed as normal, the ticket walks down
  to `retry_state = exhausted`, and the artifact appears on the operator
  stuck-list for a manual grant. Nothing is punished; the loop just becomes
  finite and visible. `12` is deliberately independent of the ordinary base
  budget and sits above the per-ticket `8`, so a single validator's local
  outage is still absorbed whole.
  A lease the **platform** revoked is deliberately exempt: repetition there is
  evidence about the platform, not the artifact, and billing the miner for it
  is the rule #460/#497 settled in the other direction.
- **Infrastructure retries back off; scoring failures reissue immediately.** A
  `scoring_error` sets `retry_after = now` (immediate reissue for another
  validator/attempt). An `infrastructure` failure instead sets an **escalating
  cooldown** — `infra_retry_backoff(infra_retry_grants)`, doubling from 2m up to
  a 30m cap — so a *sustained* provider/relay outage is retried to success
  without immediate back-to-back re-leases hammering the failing provider (an
  inference burst). Both are well short of the 6h agent-failure timeout cooldown.

## Timeout vs. explicit failure

- **Timeout** (`expire_overdue_tickets`): a lease passes its deadline unscored →
  ticket `expired`, `retry_after = deadline + RETRY_COOLDOWN` (6h).
- **Explicit fail** (`fail_job`): the validator reports terminal failure →
  ticket `expired`, `retry_after = now` (immediate reissue, no 6h wait).

### A recorded failure can describe a superseded lease

A ticket row is a *lease slot*, not an append-only attempt log. Reissue rewrites
it in place — `status` back to `issued`, `issued_at = now`, `attempt_count += 1`
— but **`failure_reason` and `failed_at` are deliberately left alone** so the
last failure stays auditable. A ticket that failed, was re-leased, and then
scored therefore reads `status = scored` *and* `failure_reason = scoring_error`
at the same time.

So never read `failure_reason` as the current state of the row. It is history
whenever either holds:

- `status = scored` — `fail_job` only accepts an `issued` lease
  (`get_open_ticket`), so a scored row's failure necessarily predates its last
  reissue.
- `failed_at < issued_at` — the failure belongs to a lease that has since been
  replaced.

`attempt_count > 1` is the corroborating signal that earlier attempts exist.
`PublicValidationAttempt` publishes all four fields for exactly this reason:
treating the kept failure as current made every retried-then-scored quorum input
render as a failure in the public submission drawer.

## When is a submission "stuck"?

A below-quorum submission is one of these retry states (surfaced per agent and
fleet-wide, see below):

| State | Meaning | Needs an operator? |
| --- | --- | --- |
| `running` | A validator holds a live ticket right now. | No |
| `retry_available` | An expired ticket is off cooldown, budget to spare; re-leases next sweep. | No |
| `cooling_down` | Expired ticket has budget but is waiting out `RETRY_COOLDOWN`. | No |
| `exhausted` | Every remaining validator burned its attempt budget; cannot advance without a grant. | **Yes** |
| `queued` | Below quorum with slots simply never leased yet. | No |

Only **`exhausted`** needs a human. The most common cause is a validator-side
infrastructure outage (e.g. a model-relay/upstream outage) that burned attempts
on failures that were not the agent's fault.

## Visibility

- **Per agent:** `GET /api/v1/admin/validation-retries/{agent_id}` — full ticket
  ledger, `automatic_retry_available`, `recovery_allowed`, `blocking_reason`.
- **Fleet-wide:** `GET /api/v1/admin/validation-retries` — every below-quorum
  submission with its `retry_state`, sorted most-urgent first, plus fleet
  `counts` per state. Filter with `?state=exhausted`. This is the triage view;
  it replaces sweeping the per-agent route one agent at a time.

## Operator recovery

For an `exhausted` submission after verifying the failure was validator-side
infrastructure:

1. Read the fleet list (or per-agent detail) to confirm `recovery_allowed:true`
   and capture the `snapshot`.
2. `POST /api/v1/admin/validation-retries/{agent_id}/retry` with the `snapshot`,
   an idempotency `request_id`, and a `reason`. This raises `manual_retry_grants`
   on exactly the minimum number of expired tickets needed to restore quorum and
   clears their cooldown. Accepted scores, screening verdicts, and ticket history
   are preserved; it is **not** a rescreen.

To recover several stranded submissions at once (e.g. the batch left exhausted
by one outage), `POST /api/v1/admin/validation-retries/batch-retry` with a shared
`reason` and one `{agent_id, request_id, expected_snapshot}` item per agent. Each
item is gated and snapshot-checked exactly like the single route; an item whose
state moved is **skipped** with a reason rather than force-granted, and all grants
commit together.

Recoveries are bounded (`MAX_OPERATOR_RECOVERIES_PER_AGENT = 4`) and audited in
`ValidatorRetryRecovery`.

## Removing a submission from the queue: withdrawal vs. eviction

Two operator routes end a submission's benchmark era. Both write the same
`ValidatorQueueWithdrawal` row — the record every queue predicate already reads —
and both preserve the submission, the miner's payment, the artifact, the
screening verdict, every accepted score, and the whole ticket history. Neither is
a rejection or a rescreen.

| | `POST .../withdraw` | `POST .../evict` |
|---|---|---|
| confirmation | `REMOVE FROM VALIDATOR QUEUE` | `EVICT LIVE VALIDATOR LEASES` |
| gate | needs enough exhausted tickets that quorum is unreachable, **and** no live lease | needs only `evaluating` and below quorum |
| live leases | refuses to act while one exists | force-expires each one, freeing the slot now |
| audit | the withdrawal row | the withdrawal row **plus** one `validator_lease_audit` row per revoked lease (`action = operator_evicted`) |
| `evicted_validator_hotkeys` | `NULL` | the revoked set (possibly `[]`) |

**Why eviction exists.** Withdrawal is a cleanup tool: by construction it only
accepts a submission that had already stopped consuming validator capacity. On
2026-07-27 a family of hanging submissions held 6 of the fleet's 12 validator
slots — every ticket running its full 90-minute lease and reporting nothing —
and withdrawal refused all of them with *"submission can still reach quorum
automatically"*. The only remedy available was to wait for each to exhaust its
own retry budget, at ~4.5 validator-hours per attempt.

**Why it does not go through the liveness gate.** `lease_liveness()` *infers*
idleness from telemetry and refuses when the evidence is absent, including the
blanket "a lease that never reported is not revocable" rule. That rule stays
exactly as strict — pre-v16 validators still omit a leased-but-quiet slot
entirely, so absence remains uninterpretable for every automatic path, and
nothing here relaxes it.

The operator route asks a different question, and since ditto-subnet#274
(v0.35.0, accepted by ditto-platform#499) it can answer it with **positive
evidence**. A protocol-16 validator announces a slot from the moment it is
claimed and leaves `ActiveBenchmarkSlot.progress` null until it has something to
say — the honest negative the gate always lacked. So *occupied and not
progressing* is now an observation, and it is the observation a hang produces.
The eviction records which of three it saw, and proceeds in all three (the
2026-07-27 case was invisible on protocol 15, and refusing to act on
invisibility is what left the operator with no move):

| audit `reason` | what the platform saw |
|---|---|
| `operator_evicted_occupied_not_progressing` | v16 says it holds the slot with nothing to report — a hang, observed |
| `operator_evicted_occupied_progressing` | the slot is visibly working; proceeds, but logged at WARNING and flagged in the audit row |
| `operator_evicted_occupancy_unobservable` | pre-v16, stale/missing heartbeat, or an unparseable blob — recorded as unobservable, not dressed up as evidence |

**Eviction never mints a no-fault retry grant.** `force_expire_lease`
compensates the miner by default, because an automatic revocation is the platform
possibly destroying a healthy run and the agent should not be billed the attempt
the reissue charges. An eviction is the decision that there *is* no reissue, so
there is nothing to offset — and granting anyway would raise the evicted agent's
attempt cap. That is not hypothetical: ditto-subnet#279 established that the
2026-07-27 leases were **misclassified, not silent**. All twelve expired `mnemo*`
tickets carry the `fail_job(reason="infrastructure")` signature —
`retry_after − deadline` of exactly +2min/+30min, the `infra_retry_backoff` base
and cap — and `infrastructure` is the no-fault class, so every hang minted a
grant, raised the cap and re-leased. `mnemox-v55` reached nine attempts against a
base budget of two with zero scores that way. The eviction route passes
`compensate=False` for exactly this reason.

**A validator mid-run on an evicted lease** finishes and posts its score into a
409: the ticket is no longer `issued` and its deadline has moved, so it fails the
k=3 gate in `submit_score` exactly like any superseded lease. Nothing reaches the
ledger.

### Seeing the run the eviction did not stop

That last paragraph is correct and it used to be invisible. Eviction releases the
platform's half of the lease at once; the container keeps executing, so for
minutes to over an hour a host is burning a full benchmark's worth of CPU on a
run that cannot produce a score — and every platform surface said the slot was
free. On 2026-07-27, nine bench-7 evictions revoked five leases across three
validators and the fleet view rendered each of those slots `Idle` beside a
"Running benchmark" badge. Read as headroom, the obvious next move (raise the
slot cap) makes it worse.

`ditto/db/queries/orphaned_leases.py` derives the missing state and
`PublicValidatorHeartbeat.orphaned_slots` publishes it. It is a read: nothing
there cancels, revokes, or re-leases.

The "was evicted" half is exact — the `validator_lease_audit` rows above. The
"still running" half comes from `ValidatorHeartbeat.claimed_slots`, **not**
`benchmark_capacity`, and that distinction is the whole mechanism. The stored
capacity is filtered at ingest to slots confirmable against a *live* ticket, and
an evicted lease has none, so an orphaned slot is always missing from
`benchmark_capacity.active` no matter what the validator reported.
`claimed_slots` is the same signed claim captured before that filter. Production
confirms it: 23 minutes after the incident, the two orphaned hosts had
`benchmark_capacity.active = []` and their orphaned run visible only there.

| state | derived from | rendered as |
|---|---|---|
| `still_running` | the claim still lists this slot, held by the evicted agent | `Evicted · still running · 23m` |
| `released` | the slot is gone from a **v16** claim, or another agent now holds it (any protocol — a slot runs one benchmark at a time) | nothing; the slot is genuinely idle |
| `indeterminate` | everything else | `Evicted · state unknown · 23m` |

The third state exists because the fleet is mixed. Through protocol 15 a
validator omits a claimed-but-quiet slot entirely, so its silence distinguishes
nothing; from protocol 16 (ditto-subnet#274, accepted by #499) it announces the
slot from the moment it is claimed. Absence is therefore evidence on 16 and only
silence on 15, and no branch guesses: the fix for a false `Idle` must not
introduce a false "still running" in the other direction.

A slot the platform has since re-leased drops out (its row already shows real
work). An `indeterminate` orphan stops being reported once its original deadline
plus a short grace has passed — the validator's own run timeout has fired by
then. A `still_running` orphan is never dropped on a timer: that is a stuck
container, and hiding it is the bug.

### Reversing a queue removal

`POST .../reinstate` (confirmation `REINSTATE TO VALIDATOR QUEUE`) puts an
operator-removed submission back in the queue in the era it was removed from.
This covers both live-lease eviction and exhausted-submission withdrawal.
Eviction shipped one-way, and that is the only reason it went unused: an evicted
miner has already paid an evaluation fee for an era an operator ended, so without a way
back a capacity decision doubles as a fine, and taking it means being sure the
miner earned one. The `mnemo*` family that prompted the route was expensive, not
shown to be malicious — a source review found self-imposed per-case deadlines, no
hang primitives, and a version history of pure latency work.

The reversal resolves the removal row (`reinstated_at`) rather than deleting it,
and writes its own append-only `ValidatorQueueReinstatement` row with its own
actor, reason and idempotency key. The eviction stays readable, and so do the
`validator_lease_audit` rows it wrote, which `GET /admin/lease-revocations
?action=operator_evicted` still returns. Every queue predicate reads a removal as
in force only while `reinstated_at` is null; the unique constraint over
(agent, era) is partial on the same condition, so a reinstated submission that
starves the fleet again can be evicted a second time.

**It restores eligibility and nothing else.** It does not resurrect the revoked
leases, reset `attempt_count`, mint a no-fault grant, or forgive a spent operator
recovery. That is the security property, not an omission: eviction refuses to
compensate precisely so it cannot raise the cap on the artifact it just evicted,
and a reinstatement that handed the cap back would turn the pair into an attempt
printer — evict, reinstate, collect — farming leases past
`MAX_AGENT_INFRA_RETRY_GRANTS` (12 per agent per era) and resetting
`MAX_OPERATOR_RECOVERIES_PER_AGENT`. Both counters are keyed on (agent, bench
version) and this route writes neither, so the cycle is budget-neutral however
many times it runs; the counts as they stood are recorded on the reinstatement
row. Handing back attempts is still `POST .../retry`, bounded and audited per
grant.

An exhausted withdrawal still needs a separate operator retry grant after
reinstatement; reversal itself never adds attempt budget. It refuses, by name,
the cases where re-admission would change nothing: the removal was already
reversed, the submission is no longer evaluating or has reached quorum or has
fallen behind the screening policy, or **the era moved on** — no validator is
ever issued a ticket for a closed benchmark version, so reinstating into one
would report success and change nothing. `reinstatement_allowed` /
`reinstatement_blocking_reason` on the detail route say which, before the
operator acts.

### Spotting one before it costs a day

A hanging submission's signature is `silently_expired` — an `expired` ticket with
no `failure_reason`, or one whose `failed_at` predates the lease it is attached
to. Both admin views publish it (`silently_expired` per ticket,
`silent_expiry_count` per submission). A submission whose silent-expiry count
climbs while its score count stays at zero is hanging, not slow; that is the
signal, and not having it is what let the 2026-07-27 incident run unnoticed.

The complementary case — an expiry that *did* report — is now readable too.
`failure_reason` is a three-value class chosen to drive reissue policy, so on
its own it says how the platform responded and nothing about what happened:
ditto-subnet#279 read twelve `infrastructure` verdicts off these rows and still
could not name which of the validator's five sandbox codes fired, because the
wire had nowhere to put it and the code survived only in a log line on the
validator host. `failure_detail` carries it. It is validator-supplied,
advisory (unsigned, drives no policy), bounded to 4096 characters, optional —
a validator predating the field simply sends nothing — and written and cleared
with `failure_reason`, so the pair is read as one report. It appears on every
admin ticket projection, which is where a triage session already looks.

That bound was 200 for one day. It was enough for a failure *code* and nothing
else, and the first time the field carried a real diagnostic message it cut it
mid-word — losing the clause that named what the platform had done to the
harness's inference requests, while leaving behind a half-sentence that read as
a complete finding. 4096 is ~16x the longest message observed and still a
ceiling: this row is written by a validator, on a hot table, from strings a
miner's harness can influence, so "unbounded" would be a storage and
log-volume liability with an adversarial input path into it. Overflow past the
cap is truncated by the *sender*, never rejected here — a 422 would lose the
whole hand-back and leave the lease to expire silently, trading the diagnosis
for the ambiguity the field exists to remove — and the sender now appends an
explicit `...[truncated, N chars]` marker so an amputated message is
recognizable as one.

Mixed fleet versions are safe in both directions. Widening a `max_length` only
ever admits more, so every validator still on the 200-char truncation (0.34.1
through 0.37.3 at time of writing) validates unchanged. In the other direction —
a widened validator against a platform not yet carrying this change — the
validator retries a 422'd hand-back once with the detail re-truncated to the
legacy 200, so the report still lands and only the tail of the message is lost.
Deploy order is therefore not load-bearing, though platform-first keeps the
full message.

## Retirement: when the benchmark generation closed instead

Everything above assumes the submission's benchmark version is still being
scored. A rollout is forward-only, so once a newer version activates, the older
one is never scored again. A submission still below quorum in the closed era is
not stuck and not slow: it is finished, and no retry can change that. Its
remaining attempts are denominated in a version for which no ticket will ever be
issued.

`SubmissionRetirement` is the terminal state for exactly those rows. It is a
sibling of `ValidatorQueueWithdrawal`, not a reuse of it, because the two answer
different questions:

| | withdrawal | retirement |
|---|---|---|
| what happened | the submission burned its slots and cannot reach quorum | the benchmark generation closed underneath it |
| scope | `(agent, bench_version)` | `(agent, bench_version)`, plus `superseded_by_version` |
| gate | requires enough exhausted tickets that quorum is unreachable | requires the era to be older than the active one and unadmitted to it |
| public status | `not_queued` | `retired` |

Retirement does **not** change scoring semantics. The `agents` row is not
touched at all, no score is created or removed, and `AgentStatus` gains no
member. The agent stays `evaluating`; the retirement row is what the queue
predicates and the public projection read.

### Eligibility

A submission may be retired only when all of these hold:

1. it is still `evaluating`;
2. its own benchmark era is strictly older than the active one;
3. it is **not** admitted to the active era by any disjunct of
   `benchmark_admission_predicate` (frozen rollout cohort, adopted carryover
   row, or audited contract refresh);
4. it is below quorum in its own era;
5. it is not already withdrawn from that era, and not already retired.

Note what is absent: retry budget. A submission with attempts left is still
retirable, because those attempts cannot be spent.

### Interaction with previous-generation carryover

Carryover (`ditto/db/queries/benchmark_carryover.py`) adopts stranded
previous-generation submissions *into* the new era. Retirement closes them out.
They are opposite remedies for the same rows, so exactly one may apply:

- **carryover first** writes a `BenchmarkRolloutCarryover` row, which is an
  admission disjunct, so clause 3 fails and the submission is no longer
  retirable;
- **retirement first** is respected by `stranded_prev_gen_candidates`, so an
  adoption pass skips retired rows. Enabling the carryover policy later can
  never silently resurrect work an operator has already closed out.

Retirement takes precedence, deliberately: it carries a named actor and a
reason, so undoing one should be an explicit operator act rather than a side
effect of flipping a policy flag. There is no un-retire route today; bringing a
retired submission back means deleting its retirement row on purpose.

### Applying it

1. `GET /api/v1/admin/retirements` previews the eligible set. Read
   `population_counts` and `finalized_prev_gen_count` before acting: "previous
   generation" is three groups, and only `partially_scored` and `never_scored`
   are ever eligible. `finalized_prev_gen_count` is the already-scored
   population, which this action never touches.
2. `POST /api/v1/admin/retirements/{agent_id}` retires one, with a `snapshot`,
   an idempotency `request_id`, a `reason`, and the confirmation phrase
   `RETIRE PREVIOUS GENERATION`.
3. `POST /api/v1/admin/retirements/batch` retires several, with one
   `{agent_id, request_id, expected_snapshot}` item each. An item whose state
   moved is **skipped** with a reason rather than force-applied, and all
   retirements commit together.

### What a miner sees

The row leaves "Waiting for scores", loses its `validator_queue_rank`, stops
being counted in the waiting total, and carries no retry chip. It stays on the
submissions page, findable by search and by direct URL, labelled
`Retired · earlier benchmark`, with copy explaining that the subnet moved to a
newer benchmark, that nothing was rejected, and that the scores it did receive
are still on file.

## The retired-era floor is terminal, never infrastructure

Benchmark versions below **7** cannot be leased or scored at all. The bound is
in the schema, not in a setting: `scores`, `confirmation_scores` and
`benchmark_rollouts` carry `bench_version >= 7` CHECK constraints, and
`validator_tickets` carries a trigger that refuses both an insert and any
update that flips a sub-v7 ticket back to `issued`. See
`MIN_SCOREABLE_BENCH_VERSION` and the
`2026_07_28_enforce_bench_version_floor` migration.

The constraints are `NOT VALID`, so every historical v2-v6 row stays present
and readable. This closes the era to new writes; it does not delete anything.

**A score submitted for a retired era returns `410 Gone` with error code
`4002`, and that is a terminal verdict.** It must never be handed back as
`fail_job(reason="infrastructure")`. Infrastructure is the no-fault class
documented above — it mints a compensating grant, raises the attempt cap and
re-leases — and a retired era is a condition that never clears, so treating it
as infrastructure re-leases forever. This is the ditto-subnet#279 shape
exactly, and it is why the status is 410 rather than 409: a conflict invites a
retry, `Gone` does not. The correct hand-back is `scoring_error`, which is
already what the canonical lane does for a `PlatformError` out of
`submit_score`.

The floor is also why a misclassification cannot loop even if one happens: the
reissue an `infrastructure` verdict asks for has to put a sub-v7 ticket back
into `issued`, and the trigger refuses it. The lease dies either way.

### In-flight leases

A lease that was already live when the floor landed is left alone rather than
force-closed. It reaches a terminal state one of two ways — the validator
reports the 410 through `fail_job`, or the overdue sweep expires it at its own
deadline, at most ~90 minutes out — and nothing can re-lease it afterwards.
Permitting the `issued -> expired` drain while refusing the re-lease is why
the ticket guard is a trigger and not a CHECK constraint: a CHECK sees only the
resulting row, never the transition, so it would have stranded every in-flight
lease in `issued` with no way to close it.
