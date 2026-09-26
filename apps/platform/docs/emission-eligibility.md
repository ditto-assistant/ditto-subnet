# Terminal-review emission eligibility

The operator gate that stops a scored artifact from earning emissions while its
own source review is unresolved, inconclusive, infrastructure-failed, or
escalated (ditto-subnet #2041, under epics #2051 and #2053).

**It ships off. Merging it cannot move emissions.** `enforcement` defaults to
`off`; with no revision stored the ledger is byte-for-byte what it was before,
the epoch pin's `ledger_digest` is unchanged, and nothing is written anywhere.

## The hole it closes

`list_eligible_ledger` filters the emission pool on `agents.status == 'scored'`,
which drops an agent parked in `ath_pending_review`. It does **not** drop what
[`ath-review-queue.md`](ath-review-queue.md) calls a **stranded hold**: an
`ath_reviews` row still `pending` while `agents.status` has moved back to
`scored`. At least three documented paths produce that state. Such a row is in
the ledger, folds into KOTH, and earns emissions with its review open. A later
rejection then makes the subnet's own past payments the dispute.

## Withheld states

One state per artifact, most specific first. Each is read from the table that
already owns it — nothing is duplicated into a new status column.

| `state` | Read from | Meaning |
|---|---|---|
| `review_rejected` | `ath_reviews.resolution = 'reject'` | Adjudicated against this artifact. Never eligible, never re-payable. |
| `review_escalated` | latest `ath_copy_court_recommendations.verdict = 'escalate'`, or `algorithm_provenance.review_kind = 'anomalous_score'` | The platform declined to decide mechanically; an operator must rule. |
| `review_inconclusive` | `reason_code` in `source-review-inconclusive`, `repeatedly-inconclusive` | Mandatory verification did not finish. **Not a finding** (#2077); the published reason says so. |
| `review_infrastructure_failed` | `reason_code` in `INFRA_AUTO_RETRY_REASON_CODES` + `PROVIDER_BACKOFF_REASON_CODES`, with a failed/expired latest attempt | Ditto's build or provider failed. **Never a miner violation** (#2051); retried automatically by `screening_infra_retry`. |
| `unresolved_review` | `ath_reviews.status = 'pending'`, any kind | The generic open hold, including a stranded one. |
| `review_missing` | no `passed` screening attempt | Only when the operator sets `require_completed_review` (off by default). |
| `awaiting_next_window` | `resolved_at >= window_start` on a `clear` | Cleared, and starting at the next window. |

`eligible` is everything else, including an artifact that was never held — which
is the overwhelming majority, and is why the default posture changes nothing.

Each withheld class has its own switch (`require_terminal_review`,
`exclude_inconclusive`, `exclude_infrastructure_failed`, `exclude_escalated`,
`require_completed_review`), so a class can be enabled or disabled on its own.

## The next-window rule

Eligibility is evaluated against a **tumbling UTC window** whose length is
`activation_window_seconds` (default 3600, the validator epoch). A terminal
clear at `t` is honoured from the first window opening strictly after `t`.

Two reasons, and neither is negotiable:

* A clear applied mid-window would move the fold's pool under validators that
  already read it — the split `ledger_pin` exists to remove.
* Honouring it any earlier than the boundary would be a retroactive grant.

**There is no back-pay and no clawback.** The only thing the gate can do is
include or exclude a row from the *next* fold. `activates_at` on the eligibility
record says when a cleared artifact starts earning; nothing anywhere describes
the withheld period as owed, because no field exists to put that in.

## What miners see

Scores stay published throughout. Three separate facts:

* **score rank** — `PublicLeaderboardEntry.rank`, unchanged. A withheld leader
  still leads.
* **provisional champion** — `PublicKothEmissions.provisional_champion` /
  `champion_reward_eligible`. A champion whose artifact is withheld keeps the
  crown and the 65% slot goes *unpaid*, not reassigned.
* **reward eligibility** — `PublicLeaderboardEntry.reward_eligibility` and
  `PublicSubmissionPipeline.reward_eligibility`: the state, a fixed
  miner-facing sentence, `reward_eligible`, `posture_satisfied`, the posture
  revision, the window, and `activates_at`.

The sentences come from one table (`STATE_REASONS`), so the board, the
submission page and Backroom cannot disagree. They carry no source, no reviewer
output, no thresholds and no cohort statistics.

## Rehearsing, then enabling

```
off  ->  shadow  ->  enforce
```

1. **Rehearse.** Write a `shadow` revision. Every ledger read now evaluates the
   gate and appends one `emission_eligibility_shadow_records` row per artifact
   per window for what `enforce` *would* have withheld — while the pool keeps
   paying exactly as before. Read it back on
   `GET /api/v1/admin/emission-eligibility` (`recent_shadow_records`,
   `effective.shadow_excluded_count`) or in Backroom
   (`get_emission_eligibility_policy`). Per artifact:
   `GET /api/v1/admin/agents/{agent_id}/emission-eligibility`, Backroom
   `get_agent_emission_eligibility`.
2. **Reconcile.** Every row in that rehearsal is a miner who stops earning at
   the flip. Clear or reject the holds first; a stranded hold needs unsticking
   before it can be resolved at all (see `ath-review-queue.md`).
3. **Enable.** Write an `enforce` revision. Withheld rows leave the pool at the
   platform's next materialization, and at the next pin for the pinned fleet.

Writes are `POST /api/v1/admin/emission-eligibility` with `expected_revision`,
an `actor`, a `reason` of at least eight characters, and the confirmation
`APPLY EMISSION ELIGIBILITY` typed verbatim. Revisions are append-only.

## Failure directions

| Situation | Behaviour |
|---|---|
| No revision stored | `off`. Pre-gate ledger. |
| Revision will not parse | `off`, logged, `source: "default"` with the unusable `revision` still reported so it is findable. **Keeps paying.** |
| Review tables unreadable on the board / submission page | No annotation; the page renders. The validator ledger resolves independently. |
| Rehearsal insert fails | Logged and swallowed. Losing a rehearsal row is an observability loss; failing the ledger read would zero every miner. |
| Ledger read fails and the last-known-good snapshot is served | The posture marker is replayed with the entries it filtered, never re-resolved. |

Every direction is the same one: an error never withholds.

## Wire and storage

* `LedgerResponse.reward_eligibility_mode` — `"enforce"` or absent. Additive and
  optional, keyed into the pin's `served` context **only** when enforcing, so
  every pin taken off or in shadow keeps its pre-#2041 digest. The pool is
  filtered platform-side either way, so a validator that ignores the field folds
  correctly.
* `emission_eligibility_settings_revisions` — append-only posture, shaped like
  `burn_settings_revisions`.
* `emission_eligibility_shadow_records` — append-only rehearsal, unique on
  `(agent_id, bench_version, policy_revision, window_start)` so a 30-second
  validator poll cannot grow it.
