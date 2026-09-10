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
| `deferred_source_review` | the score-qualified source review, in enforce mode |
| `anomalous_score` | the out-of-band composite escalation at finalization (bench v12+), in enforce mode |

`review_kind` postdates the holds it describes, so the oldest rows carry no key
at all and both the projection and the filter treat a missing or unrecognized
value as `copy`. Those two rules must stay in step: a filter matching only the
literal string would drop legacy rows while every row it returned still said
`copy`, which is an omission with nothing on its face to reveal it.

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
