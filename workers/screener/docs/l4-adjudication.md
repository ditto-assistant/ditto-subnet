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
it relied on. Under policy v13, it can instead request operator review when
mandatory verification is incomplete; the host keeps that submission held.

It runs `z-ai/glm-5.3-flash`. The expensive discovery already happened
upstream; what remains is applying a written standard to named locations.

## Bounded decision packet

When the upstream ledger has retained source leads, the host preloads exact
`path:line` excerpts and sends one decision-only packet. That packet carries
the archive SHA-256, the complete bounded notes ledger, the upstream finding,
the applicable policy prompt, and every preloaded source line. The archive
inventory is omitted on this path because the court has no discovery tools and
cannot cite an inventory entry as source evidence. The inspectable path for a
review with no ledger still receives the inventory and read-only tools.

A decision-only packet above 64,000 bytes is held as
`adjudicator-packet-too-large` before any model request. The host does not trim
source, notes, or policy text to fit. Existing unread-lead and citation checks
still apply. An active response that times out or ends without a complete tool
call is held without replaying the same packet; only a connection failure
before response data or an explicit provider fault gets one transport retry.

The packet size and retry change do not establish verdict accuracy. Before
enforcing a release, replay exact SHA-bound held artifacts in report-only mode
and compare decisions with independent source review, citation coverage,
completion latency, request count, token and cost use, and escalation subtype.
In particular, count `adjudicator-packet-too-large` and
`adjudicator-evidence-incomplete` separately. Keep the change in shadow if
those checks are unavailable or reveal a new false clear or reject.

An explicit `request_operator_review` model tool call is recorded as
`adjudicator-operator-requested`. Host-detected missing source coverage remains
`adjudicator-evidence-incomplete`; transport, timeout, and malformed-tool
failures keep their distinct codes. All of these outcomes remain holds. The
operator-request code does not certify that evidence was actually missing.

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

Any refusal produces `decision: "escalate"`. The v13 `request_operator_review`
tool also maps to that host-controlled hold. This path is available even when
the model request requires a tool call, so incomplete verification cannot be
forced into CLEAR or REJECT by the transport contract.

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
- `enforce` — Platform may resolve a policy v12 or earlier decision terminally.
  A v13 source-only `clear` or `reject` remains quarantined even under this
  setting. Both local and Targon paths retain the adjudication as review
  evidence; an older worker's signed v13 terminal result is refused.

The worker produces an adjudication in both `shadow` and `enforce`. The
authority to ACT on one is resolved by Platform from the current settings
revision, not trusted from the payload, so a screener running a stale revision
cannot release or reject anything the operator has not switched on.

### Lifting the v13 hold

The v13 fence is intermediate. A source adjudication alone does not attest the
build, served runtime, or private checks. Lift it only in a separate reviewed
change after the trusted replay runner and sealed private package are deployed:

1. Finish the protected verifier bootstrap and apply its exact approved binary
   plan. Keep the package inaccessible to miners and the reviewer model.
2. Emit signed, trusted build, runtime, and private verification receipts bound
   to the agent UUID, artifact SHA-256, attempt ID, policy version, and screened
   image digest. Platform must check the receipt set against the claimed attempt
   and refuse missing, mismatched, stale, or untrusted receipts.
3. Change both local and Targon admission paths and the Platform result gate in
   one reviewed patch. Test `clear`, `reject`, and `escalate` with receipt gaps,
   altered identities, and replayed attempts. Preserve policy v12 behavior.
4. Run one report-only exact-artifact canary, compare the signed receipts and
   independent source review, then explicitly authorize terminal enforcement.
   Capture the signed fleet descriptor digest before deployment for rollback.
5. For held miners, use an audited, bounded rescreen batch: preview exact
   quarantine IDs and decisions, apply that identical guarded plan, and reread
   each new attempt and board status before a decision. A manual Backroom ruling
   remains a separate exact-evidence operator action.

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
