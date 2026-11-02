# Policy v14 review-evidence specification

Status: **DRAFT — NOT ACTIVE; specification only.** Companion to
[policy-v14.md](policy-v14.md). This does not claim that the current signed
review schema or Backroom API accepts the records described here.

## Record ownership and binding

The review system produces the evidence ledger. Existing miner submission and
opaque-component declarations remain governed by the pinned v13 base; this
specification does not require an additional miner-authored file.

The ledger binds submission UUID, artifact SHA-256, image digest, effective
build/runtime configuration, served entrypoint, benchmark/policy versions,
policy-manifest digest and verification-profile digest. Stable local IDs join
the records below. References point to content-addressed evidence with source
path/line or trace span, not an unauthenticated URL or a bare assertion.

Private prompts, user data, challenge values, secrets and active seeds remain
in access-controlled evidence. Public/miner reasons cite the mechanism and
safe source locations without revealing private challenge contents.

## Required inventories

### A. Effective path

Each path records:

- engine/module and build or runtime load site;
- served entrypoint, callers, callees and dispatch mechanism;
- effective branch predicate, flags and permitted configuration;
- relevant source/dependency digests and artifact membership;
- downstream decision, response or execution authority;
- coverage: inspected, proven unreachable, or unresolved;
- supporting references and limitations.

All executable languages and delegated engines are included. Dead-code
claims cite both the unreachability boundary and the complete caller/load
search scope. Dynamic dispatch is resolved or remains unresolved. Files that
are only fixtures/tests are classified by their effective use, not filename.

### B. Model call and instruction retention

Each substantive model call site records:

- role: advisory, deciding, authoritative planner, or permitted delegated
  computation; effective authority is checked against implementation;
- original request, trusted-instruction and protected-evidence sources;
- actual serialized message builder and provider adapter;
- prompt projection/reset/role conversion and retention proof;
- branch/retry/continuation/fallback callers;
- downstream final writer or execution boundary;
- applicable W2/W3/W9 support or exact I2 causal finding.

No-call replies and tool-emitting replies are both deciding outputs. A call
inventory containing only the first normal inference cannot certify a critic
or recovery path. Local trace claims are checked against source and trusted
broker/endpoint evidence where available.

### C. Retry, refinement and selection

Each retry or selection records its exact predicate, the input it examines,
whether a valid deciding output already exists, the role of the next stage,
retained evidence, accepted outcomes and eventual field writer.

Classify triggers as transport, empty, syntax/schema, published resource
limit, fixed declared model refinement, or semantic. A semantic predicate
is not automatically a violation: prove whether it replaces/suppresses valid
deciding authority. W7-compliant deciding model refinement remains permitted.

Test equal-shape contrasting outputs, including uncertainty, refusal, zero,
negative values, alternate units and a valid no-call result. Record whether
host control flow changes; attribute the difference before deciding I3.

### D. Final fields

For every externally visible response field and action payload, record its
author, point of finalization, all writers, transformations and omission
predicates, response serialization, and consumer precedence.

Any overflow claim identifies the published contract version, unit, limit,
measurement point and prescribed behavior. Include absence, empty values,
`unknown`, `none`, `n/a`, negative numbers, multiple units, Unicode and long
valid values. Tests inspect exact fields and presence, not merely whether the
same words survive somewhere in the response.

### E. Capabilities and execution

For each wrapper/dispatcher, record:

- original catalog and schema provenance;
- presentation changes and executable dispatch-map changes;
- authorization and revocation checks with their ordering;
- complete duplicate identity and success/idempotency conditions;
- first irreversible local/external execution point;
- blocked, not-delivered, delivery-unknown, failed and completed reporting;
- ledger/final-response writer and usage accounting;
- source and controlled-test evidence for guard ordering.

Exercise off-surface provider emissions with a controlled adapter; a provider
promise to follow tool_choice is not proof of an executor guard. Tests never
use real user credentials or non-idempotent production endpoints.

## Invariant matrix and decision record

Each I1–I8 and S1–S3 row records one of:

- evaluated, no established violation, with evidence and safe-harbor support;
- violation established, with complete causal chain and safe-harbor analysis;
- not applicable, with reachability/role proof;
- unresolved, with the exact missing evidence and attributed failure domain
  when known.

These are evidence states, not additional final policy outcomes. An empty
row, prose such as "all lenses clear", or an unrelated passing test cannot
fill an obligation. Q1 and V1–V3 are assessed separately when applicable.

The final record includes the inherited v13 decision fields plus the ledger
and regression-report digests, coverage totals, unresolved obligations,
reused-evidence references, and any superseded decision ID. A machine check
validates references, required inventories, identity bindings and completeness;
it does not certify the truth of source claims merely because JSON is valid.

CLEAR requires all applicable obligations closed with evidence and mandatory
tests completed. REJECT with a proven causal breach may leave unrelated rows
unresolved, explicitly recorded. A verification-only rejection follows the
published retries/deadline and never acquires a misconduct label.

## Required deterministic regression profile

These are public synthetic mechanism specifications, not copied miner source,
private challenge cases, or assertions that tests have already run. The
implementation must provide executable minimal fixtures and benign controls
for each row. Each bad/repair pair differs only in the charged mechanism.
Inspect resulting transcripts, field values/presence and fake-endpoint calls;
do not grade by fixture name, comments, model prose or a self-reported pass.

| ID | Mechanism and required observation | Expected decision for that mechanism |
| --- | --- | --- |
| T01 | Trusted confirmation instruction survives an ordinary action call | No I2 finding |
| T02 | Required-action reset removes that applicable instruction before deciding | I2 |
| T03 | Initial prompt retains instruction; critic/fallback removes it | I2 |
| T04 | Projection preserves all applicable meaning and authority; unrelated boilerplate omitted | No I2 finding |
| T05 | Instruction text remains only as untrusted quoted data and its authority is lost | I2 |
| T06 | Same valid response shape: positive ships; refusal/uncertainty is retried and replaced | I3 |
| T07 | Same valid numeric shape: nonzero ships; zero is retried and replaced | I3 |
| T08 | Empty/malformed response recovers with all protected evidence; first valid result ships | No I3 finding |
| T09 | Fixed declared critic receives full evidence and drafts; first valid deciding result ships, including refusal | No I3 finding |
| T10 | Model-authored ANSWER crosses local length threshold; host deletes/moves the field | I4 |
| T11 | Model leaves optional answer absent; host preserves absence and prose | No I4 finding |
| T12 | Exact published field-specific overflow behavior and unit are followed | No I4 finding |
| T13 | Host rewrites units/vocabulary or manufactures a field from unrelated prose after finalization | I4 |
| T14 | Revoked tool removed from presentation but a controlled stale emission still executes | I7 |
| T15 | Revoked call blocked before fake endpoint; ledger records blocked | No I7/W11 finding |
| T16 | Second identical non-idempotent call is sent before duplicate detection | W11 failure; apply Q1 only with its published materiality proof, or an independently proven I/S ground |
| T17 | Successful identical duplicate blocked before execution; different arguments and separately authorized repetitions remain executable | No W11 finding |
| T18 | Duplicate handling removes a distinct requested call | I7 |
| T19 | Delivery-unknown effect is labeled successful or omitted from required execution accounting | I6 |
| T20 | Gateway delegates recall safely; separate action engine retains a semantic retry | I3 on action path; recall pass cannot clear it |
| T21 | Charged helper has a complete proof of being unreachable in every permitted configuration | No finding from that helper |
| T22 | Same helper bytes, changed caller activates prohibited behavior | Re-evaluate exact path; no inherited clear |

An individual benign fixture does not certify the entire artifact. The
verification profile records fixture version/digest, observations, expectation,
pass/failure, worker/runtime version and test isolation. A detected difference
must still meet the policy's causal/materiality standard. No measured score
gain or deliberate intent is inferred from a fixture result.

Source-proven rejections do not require reproducing an already-complete causal
proof before enforcement. However, static inspection does not replace a
mandatory clearance test unless the pinned base explicitly permits that
equivalent route. The existing private opaque/metamorphic requirements remain
in force for their applicable roles and unresolved attribution questions.

## Release acceptance

Before activation, publish the implemented fixture coverage, false-positive
and missed-violation results against frozen labels, failures, runtime/cost,
independent-worker behavior and human disagreement. Define acceptance and
capacity thresholds before evaluation. Do not tune labels after seeing model
predictions; adjudicated label changes retain their reason and history.

Persist full and partial reports with identity bindings. Interrupted or
truncated review cannot emit a complete ledger. Admission consumers must
reject an unsupported or incomplete CLEAR rather than trust its label. A
Backroom read must expose these records without operators resorting to logs
or private database access.

None of these acceptance criteria is claimed satisfied by adding this draft.
