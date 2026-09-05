# L4 automated adjudication of a held source review

L1, L2 and L3 answer "is there something here". L4 answers "does it clear the
bar", and it exists because the first question is cheap and the second is not:
the reviewer is instructed to record a concern the moment it sees one, and the
finding contract over-flags a documented set of legitimate patterns. Every
result those layers cannot resolve becomes an operator hold, and the operator
is the bottleneck.

The adjudicator receives the notes ledger the earlier layers accumulated, the
upstream finding if there is one, re-reads the source those notes point at with
the same read-only tools, applies the published adjudication doctrine, and
returns `clear` or `reject` with a miner-visible reason and the `path:line` set
it relied on.

It runs `z-ai/glm-5.3-flash`. The expensive discovery already happened
upstream; what remains is applying a written standard to named locations.

## What it adjudicates

Only an outcome that would otherwise WAIT:

- a hold-bound finding (a medium/high risk result), or
- a budget-terminated review whose ledger did not admit it.

A clean review already has its answer. A `retryable_infra` failure has no
evidence to weigh and is not miner conduct, so it is retried rather than
judged — and it is settled before adjudication is even considered.

## Why a small model is safe here

The safety argument is host verification, not model size. The decision itself
is cheap to check, and every check is mechanical:

| refusal | what it catches |
|---|---|
| `uncited-decision` | a verdict resting on nothing |
| `cited-unknown-member` | a path the archive does not contain |
| `cited-unread-source` | a location this adjudicator was never served |
| `inadmissible-citations` | only comments, imports, braces, or test paths |
| `verdict-contract-failed` | no published basis from the closed vocabulary |
| `adjudicator-failed` / `adjudicator-unavailable` | the run itself did not complete |

`cited-unread-source` is the load-bearing one. The repository tools report the
line numbers they serve, so the host reads the served locations back out of the
tool results rather than inferring them from the request. "You may only cite a
line you have read" is therefore enforced rather than requested, and it
subsumes a bounds check: a citation past the end of a file was necessarily
never served either.

Any refusal produces `decision: "escalate"`. When the ledger is empty, that
escalate is terminal: the policy module quarantines, and an operator sees the
hold. An empty-ledger clear is not "no proven breach" — it is unseen source,
and it is how scored agents were admitted in ~80s with zero L2 notes.

When the court recorded notes and then refused (timeout, malformed citations,
unavailable model), the host still converts that escalate into a
`no_proven_breach_before_deadline` clear. Reject remains fail-closed: it
needs verified executable citations. A miner is not banned because the
reviewer timed out after inspecting named locations.

## Closed decision vocabulary

A reject must name the policy-v10 invariant it breached (`SourceReviewInvariant`).
A clear must name the published court false positive it relies on
(`AdjudicationClearClause`) — the same release-with-a-refutation classes the
operator review rules recognise. An adjudicator that has to name one of these
cannot free-associate its way to a decision, and an operator reading the audit
trail can check the claim against the same published list.

## Posture

`adjudicator_mode` is an operator setting, default `off`.

- `off` — no adjudicator is constructed.
- `shadow` — decisions are produced and recorded as quarantine evidence; the
  hold stands.
- `enforce` — Platform resolves the decision terminally.

The worker produces an adjudication in both `shadow` and `enforce`. The
authority to ACT on one is resolved by Platform from the current settings
revision, not trusted from the payload, so a screener running a stale revision
cannot release or reject anything the operator has not switched on.

## Doctrine drift

The system prompt encodes the operator adjudication standard maintained in
`.agents/skills/backroom-review/references/review-rules.md` and
`review-bar.md`: the policy-v10 invariants, the two-limb refusal and
production-engine tests, and the known false-positive catalogue. Those
documents remain the human-facing source of truth. When a ruling changes there,
change the prompt in `ditto_screener/adjudicator.py` and bump
`ADJUDICATOR_PROMPT_REVISION` in the same commit — the revision is recorded on
every decision, so an audit can tell which standard a given adjudication was
made under.
