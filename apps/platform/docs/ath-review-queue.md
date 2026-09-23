# The ATH review queue

The operator queue for SN118 is **`ath_reviews` rows with `status = 'pending'`**.
Nothing else is. This document exists because two other surfaces look like the
queue and are not, and because the queue's two status columns can disagree.

## What the queue is

`GET /api/v1/admin/copy-reviews?status=pending` — every unresolved ATH hold,
ordered by `COALESCE(reopened_at, opened_at)`. Backed by `ath_reviews`
(`ditto/db/models.py`), served by
`ditto/api_server/endpoints/admin_copy_review.py`, and reached from the Backroom
MCP as `get_screening_review_queue`.

Three kinds of hold land here, distinguished by
`algorithm_provenance.review_kind` and filterable with the `review_kind` query
parameter:

| `review_kind` | Opened by |
|---|---|
| `copy` | the anti-copy gate at quorum, or a manual operator hold |
| `benchmark_overfit` | the transform audit |
| `deferred_source_review` | the score-qualified source review, in enforce mode, or the top-five integrity double-check (`algorithm_provenance.trigger = integrity_double_check`) |
| `anomalous_score` | the out-of-band composite escalation at finalization (bench v12+), in enforce mode |

`review_kind` postdates the holds it describes, so the oldest rows carry no key
at all and both the projection and the filter treat a missing or unrecognized
value as `copy`. Those two rules must stay in step: a filter matching only the
literal string would drop legacy rows while every row it returned still said
`copy`, which is an omission with nothing on its face to reveal it.

## The top-five integrity double-check

`queue_policy_settings.deferred_source_review.integrity_double_check_mode`
(`off` / `observe` / `enforce`, independent of `mode`) gives every top-five
row one stronger deep review. It covers rows that already passed the full
pre-score screen:

```
eval -> top five -> integrity double-check -> clear or reject
```

- **Trigger.** Every canonical ledger mutation re-reads the same-version
  ledger (`_evaluate_and_record_integrity_double_check`,
  `endpoints/validator.py`). A top-five row is skipped if its agent already
  has a `deferred_source_review` lifecycle or an enforced
  `audit_kind: integrity_double_check` score-audit marker. The marker survives
  a later copy reopen of the agent's single review row. Rows already held for a
  post-score deep review keep their rank slot through their evidence
  composites, so holds cannot cascade down the board. `observe` appends one
  unenforced audit record per agent and holds nothing.
- **Hold.** `enforce` opens the normal pending `deferred_source_review` hold
  with the double-check reason and `trigger: integrity_double_check`, and
  appends the enforced marker. Reason, actor
  (`platform:integrity-double-check`), and algorithm version
  (`integrity-double-check-v1`) differ; the lifecycle does not.
- **Stronger posture.** `claim_screening_attempts` binds the deep pass to the
  latest screener review revision in scope `integrity-double-check`. No worker
  heartbeats under that scope, so writing it never changes the fleet posture.
  The screener reads it through `review_settings_override`, and
  `submit_screen_result` accepts only that exact binding. Set a stronger
  `l2_model`, `l2_always_escalate: true` (L2/L3 run even when L1 certifies a
  clear), and larger budgets there. With `enforce`, a mechanically admitted
  top-five row's deferred pass takes the same posture.
- **Fail closed.** Platform refuses `integrity_double_check_mode=enforce` (409)
  until that scope holds an `enforce` revision with `l2_always_escalate`,
  `l3_enabled`, and the `l1_l2` manifest. Worker compatibility also requires
  `timeout_seconds <= 900`, `max_steps <= 20`, and low or medium critic
  reasoning. If the posture later becomes unusable, or the claimant
  cannot bind one (a legacy worker or the platform-owned Targon lane), the
  claim query leaves double-check holds unselected. They stay pending and
  visible here and cannot starve other screening work.
- **Exit.** This is the same deferred lifecycle: a clean pass restores
  `scored`/`live` and resolves the review as `clear`, an adjudicated reject on
  an enforcing posture rejects, and anything else stays a pending operator hold
  carrying `deep_review_result`.

## A reopened hold's active reason is not its original reason

`ath_reviews` keeps **one row per agent** for its whole life, and the reopen
path cannot rewrite what it supersedes:

- `original_reason` is immutable. `resolve_copy_review` compares it against
  `agents.review_reason` and answers `409 agent hold reason no longer matches
  review` when they disagree, so a reopen that edited it would break its own
  exit.
- `resolution` / `resolution_reason` must be NULLed on reopen to satisfy
  `ath_reviews_lifecycle_check`, which forbids a resolution on a `pending` row.

So after a guarded reopen the only durable record of the *current* reason, and
of the decision that was withdrawn, is the append-only `ath_review_actions`
ledger: the newest `reopen` action carries the reconsideration reason, and the
`clear` / `reject` action before it carries the decision it withdrew.

`ditto/api_server/ath_review_state.py` is the single projection rule. Both the
public activity page and the operator queue / audit endpoints derive through
it, so a pending appeal reads the same on every surface:

| Field on `original` (`hold` in Backroom) | Meaning |
|---|---|
| `reason` | Why the submission is under review **now** |
| `reason_source` | `original_hold`, or `reconsideration` after a reopen |
| `superseded_reason` | The original hold reason, preserved |
| `superseded_resolution` | `clear` / `reject` — the decision the reopen withdrew |
| `superseded_resolution_reason` | That decision's own public reason |
| `superseded_at` | When the reopen superseded it |

A `pending` row never carries a live `resolution`, and the `superseded_*`
fields are history. Quoting one back to a miner as a standing finding is a
factual error: `get_screening_review_queue` published exactly that for
lets_635 v1, whose I5 rejection had been withdrawn as unsupported.

Nothing is deleted or rewritten to produce this. `original_reason`,
`agents.review_reason`, and the full action history are unchanged, and the
audit endpoint still returns the whole `action_history` chain.

## Precedents are the resolved holdings

`GET /api/v1/admin/copy-reviews/precedents` is the court reporter, not the
queue. It defaults to `status=resolved` and searches `original_reason`,
`resolution_reason`, agent name, version, and miner hotkey. Filter with
`resolution=clear|reject` and `review_kind`. Omit `q` to page newest holdings
first. Backroom reaches it as `search_ath_precedents`. The static path is
declared before `/copy-reviews/{agent_id}` so `precedents` is never parsed as
a UUID.

## Two surfaces that are not the queue

**`GET /admin/screening-quarantines?status=active` is effectively always
empty.** The platform actor `platform:deferred-source-review` auto-resolves
each quarantine to `rescreen` within milliseconds of it being raised. Active
quarantines are a transient screener state, not operator work.

**`GET /admin/screening-submissions` is the agent table, not the queue.** It
pages every agent ever submitted; enumerating holds from it means sweeping the
whole table and filtering client-side. It is also the surface where the
divergence below shows up as an apparently wrong `agent_status`.

## `agents.status` and `ath_reviews.status` can disagree

They are separate columns in separate tables, kept in step only by code
discipline, and `agents.status` is read live on every request — there is no
cache in the Backroom Worker for this path, no HTTP cache header on
`/api/v1/admin/*`, and no view or trigger over `agents`. A `agent_status` that
looks stale is therefore not stale: it is the true current value of a column
that genuinely moved out from under a still-pending review.

At least three paths produce that state:

- **`resolve_review()`** (`ditto/db/queries/agents.py`), the owner-only CLI exit
  behind `scripts/resolve_review.py`, sets `agents.status` to `scored` or
  `banned` and **never touches `ath_reviews`** — it predates the table and was
  not wired into it. This reproduces the symptom exactly.
- **Deep-review park → release → rescore.** A held agent with a pending
  `deferred_source_review` stays claimable for deep review; five expiries park
  it as `quarantined` (`ditto/db/queries/screening.py`), an operator release
  moves it to `evaluating`, and the next quorum writes `scored`. None of those
  steps consults the pending review.
- **`refresh_benchmark_contract`** (`admin_quarantine.py`) guards on zero
  accepted scores at the *active* bench version, which a hold from a prior
  generation satisfies, and moves the agent to `screening_failed`.

Once diverged the state is sticky in both directions:
`_record_deferred_review_decision` returns early when a pending review already
exists (`endpoints/validator.py`), so the hold is never re-applied, and
`resolve_copy_review` answers `409 agent is no longer held` when
`agents.status != ath_pending_review`, so the review cannot be closed through
the API either.

**This is why every `AdminCopyReviewItem` carries `agent_status`.** A pending
row reading anything other than `ath_pending_review` is a stranded hold, not
queue work: it needs unsticking, and attempting to resolve it will 409. Reading
it off the queue row is the difference between seeing that in one call and
reconciling a listing against a per-agent lookup.

To find stranded holds directly:

```sql
SELECT r.agent_id, a.status AS agent_status, r.status AS review_status,
       r.algorithm_provenance->>'review_kind' AS review_kind,
       COALESCE(r.reopened_at, r.opened_at) AS opened_at
FROM ath_reviews r
JOIN agents a USING (agent_id)
WHERE r.status = 'pending' AND a.status <> 'ath_pending_review';
```

Reconciling them is deliberately not automated. Winner-take-all makes a
false-positive hold expensive for an honest miner and a false clear expensive
for the subnet, so the exit from a hold stays an operator decision. The
**copy-hold triage court** (below) is the scoped exception: it triages, and
only an operator resolution changes state.

## The copy-hold triage court

`CopyHoldCourt` (`ditto/api_server/copy_hold_court.py`) is an in-process
platform loop that periodically triages every pending copy-kind hold and
records one **non-authoritative recommendation** per (review, settings
revision) in `ath_copy_court_recommendations`: a verdict (`clear` / `reject` /
`escalate`), a hold class, the miner-visible reason, citations, and evidence.
A recommendation never changes `agents.status` or the review's resolution.

Classification is data-verified, not reason-string regex. The court loads the
matched reference (`AthReview.original_duplicate_of`) and re-checks sha256 /
normalized-source identity before any mechanical verdict:

- `rejected_resubmission_byte_identical` — the upload's sha256 equals the
  rejected ancestor's. "Check whether the cited behavior was removed" is false
  by construction; the recommendation cites the ancestor's own resolved reject
  reason. Mechanically decidable.
- `rejected_resubmission_repack` — normalized-source identity. Same rule.
- `near_duplicate` and `rejected_resubmission_cross_miner` — not mechanically
  decidable; the court records an `escalate` with the hold evidence. The
  escalation itself is always recorded whenever the court runs, because it is
  operator evidence rather than a resolution.
- Any evidence-gathering failure escalates. Fail closed.

Posture lives in `copy_court_settings_revisions`, written through
`POST /admin/copy-court/settings` with the confirmation
`APPLY COPY COURT {MODE}` and an `expected_revision` guard. The master
`mode` caps every per-class mode; classes start `off` and move
`off → shadow → enforce` one at a time after shadow-vs-operator calibration.
In enforce mode the court resolves through the same guarded callable an
operator uses, with actor `platform:copy-hold-court` and the recommendation
id cited in the resolution reason so the append-only `AthReviewAction`
chain shows the court basis. Enforce is never bulk and never touches
stranded holds.

Reads: `GET /admin/copy-court/recommendations` (Backroom MCP
`list_copy_court_recommendations`) pages the shadow feed, newest first,
`pending_only` by default; `GET /admin/copy-court/settings` is the posture
and its history (Backroom MCP `get_copy_court_settings`).

## Generation is not a queue filter

`GET /admin/copy-reviews` also takes `generation`, which selects reviews by
whether the held agent has a score at a given benchmark version. It defaults to
`active` for the console's cohort view, and that default hides real queue work:
a copy hold opened at upload has no scores at all, and a hold that survived a
benchmark rollout has none at the new active version. Both are still waiting for
an operator. The MCP queue tool pins `generation=all` and does not expose the
parameter.

## Withdrawing a precautionary hold

`clear` certifies the artifact and `reject` records a violation. Neither is
the right exit when an operator-opened manual hold (`snapshot =
manual-admin-hold`) no longer has a supported opening rationale.
`POST /admin/copy-reviews/{agent_id}/withdraw/preview` then
`POST /admin/copy-reviews/{agent_id}/withdraw` records `resolution = withdraw`.

The preview binds the review id, pending state, agent UUID, artifact SHA-256,
score count, agent status, correction reason, and the resulting public crown.
Execute re-reads those guards and refuses a stale or repeated request. The
original reason, opener, timestamps, score rows, and action history stay.
Score and rank presentation return to the pre-hold status. Reward eligibility
does not: until the terminal exact-artifact gate in #2041 can decide this
exact artifact, the withdrawal stays out of the emission pool. A sibling
artifact's clear or reject is not consulted. Automated holds still leave
through `clear` or `reject`. Withdrawals are omitted from the precedent search.
