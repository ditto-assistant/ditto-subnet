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
for the subnet, so the exit from a hold stays an operator decision.

## Generation is not a queue filter

`GET /admin/copy-reviews` also takes `generation`, which selects reviews by
whether the held agent has a score at a given benchmark version. It defaults to
`active` for the console's cohort view, and that default hides real queue work:
a copy hold opened at upload has no scores at all, and a hold that survived a
benchmark rollout has none at the new active version. Both are still waiting for
an operator. The MCP queue tool pins `generation=all` and does not expose the
parameter.
