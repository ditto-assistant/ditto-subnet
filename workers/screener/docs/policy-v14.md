# SN118 source-review policy v14: complete decision-path evidence

Status: **DRAFT — NOT ACTIVE; runtime support and activation not implemented
by this document.** This is screening/review policy 14, not benchmark 14.

V14 makes completeness of a clearance auditable. It does not add a new
prohibition on ordinary model critique, prompt augmentation, retrieval,
calculators, or permitted model planners. Dropping applicable trusted
instructions, semantic retries of valid deciding output, unauthorized final
field changes, and execution after revocation already violate v13.

## Normative base and version boundary

V14 incorporates the following complete documents at repository commit
`d329d4faaa2464f8514992775dd3673945cdff40`, with the additions below:

| Document | SHA-256 of UTF-8 file bytes |
| --- | --- |
| [Policy v13](policy-v13.md) | `bd6cf0f9934cfcda7561a704e5a7b25196926cc06d6e57dd6036fe250946914c` |
| [Opaque verification](policy-v13-opaque-verification.md) | `5f317537499a085d679979caf7c4d9c8908bdc32c4d100701e8e51609adec867` |

The base is content-pinned, not a floating reference. Its W1–W16 safe harbors,
I1–I8 and S1–S3 integrity/security standards, Q1 materiality standard, and
V1–V3 verification-failure distinctions remain normative. Historical status
and activation records in the base do not activate v14. A release must bundle
the exact base bytes with this policy and the
[v14 review-evidence specification](policy-v14-review-evidence.md), and bind
all four digests in its policy manifest. Any normative edit requires a new
manifest revision and explicit applicability record.

An already-proven v13 violation is decided under v13 today. V14's additional
review records cannot be demanded retroactively from a v13 submission or used
to relabel its missing documentation as misconduct. V14 applies only through
an explicit activation and new or explicitly versioned rescreen attempt.

## What v14 changes

| Review obligation | Required evidence before CLEAR |
| --- | --- |
| All effective implementations | Build/entrypoint/configuration map, including language gateways and delegated engines |
| Every deciding stage | Call-site inventory and trusted-instruction/evidence retention map |
| Every retry and selector | Trigger predicate, authority before/after, and final writer |
| Every finalized field | Writer inventory and complete transformation/serialization chain |
| Every executable capability | Presentation, authorization, revocation and duplicate guards traced to the execution boundary |
| Every unresolved lead | Evidence-backed disposition; an unread path is not a pass |
| Every reused result | Exact identity, configuration, policy and dependency equivalence with current reachability checked |

These are obligations on the review system. Miners need not author a new
review ledger or sidecar. Existing published submission and opaque-component
requirements remain unchanged. A platform failure to produce its own evidence
is not a miner documentation failure.

## R1: complete effective-path inventory

Starting from the submitted build and served entrypoint, identify each
effective engine, gateway, dispatcher, model adapter, tool wrapper, recovery
handler, response writer and executable dependency. Record caller and callee
boundaries, branch predicates, feature/build flags and permitted environment.

A Python tool engine delegating memory requests to Rust has at least two
decision paths. Clearing the Rust writer says nothing about Python no-call
recovery. A same-byte module ported behind a different gateway requires a new
reachability assessment. A function is unreachable only with a cited closure
from the effective configuration, not because a name search found no obvious
caller. Dynamic dispatch or an opaque dependency requires the applicable
source/runtime evidence; a label does not close the obligation.

Coverage records distinguish inspected, proven unreachable, and unresolved
paths. Unread or truncated source, missed pages, missing dependencies, budget
exhaustion and unsupported languages remain unresolved. An established
violation may end a review in REJECT without inspecting unrelated paths;
CLEAR requires the complete obligation inventory and mandatory verification.

## R2: trusted instructions at every deciding call

V13 W2 remains the rule: each deciding answer/action call receives the original
request and all applicable trusted system instructions, safety/authorization
constraints, scope, dates and output/execution restrictions. This includes
planners, tool turns, continuation, critic, retry, fallback and finalization.

The reviewer traces the actual serialized model input, not merely the initial
prompt builder. Enumerate every transcript reset, compact prompt, role
conversion, context digest and adapter preamble. For each transformation,
identify where each applicable instruction and protected record survives.

Adding compatible miner guidance is allowed. A meaning-preserving projection
is allowed under W2. A fixed prompt independent of incoming instructions is
not evidence that their meaning survived. Quoting a restriction as untrusted
data or adding contrary instructions does not preserve its authority.

An I2 finding still supplies all seven v13 proof elements: exact identity,
specific applicable instruction/evidence, entry into the protected input,
removal point, resulting deciding input, lost material alternative, and
reachable path. Omission of irrelevant material alone is not a violation.

Wire comparison is a future enforcement implementation, not an implemented
v14 gate. Missing a literal substring is not sufficient when a valid
projection preserves meaning; including the substring is not sufficient when
it is demoted, contradicted or sent only to a non-deciding call. No new
byte-for-byte system-prompt rule is introduced by this draft.

## R3: distinguish recovery from semantic retry

Inventory every condition that schedules another inference, chooses a draft,
declares completion, or replaces an answer. Record both the trigger and the
writer that ultimately serves its result, including branches before any tool
executes and after partial execution.

Under W7, malformed/empty/schema-invalid output may recover, and a declared
genuine critic may decide a final answer from the complete request, evidence
and drafts. The host may not condition replacement of a valid deciding
response on refusal, uncertainty, a zero value, preferred vocabulary, units,
expected answer, or an expected call. Calling a completed reply a "draft",
"quality failure" or "review candidate" does not change its actual authority.

Compare equally well-formed outputs that differ only in meaning. If host
control flow ships the positive result but retries the refusal or zero result,
record the semantic predicate and authority change. A later model being free
to disagree does not cure that host selection. Conversely, a fixed declared
critic stage whose first valid deciding result always ships must not be
rejected merely because it can correct an earlier advisory draft.

## R4: finalized field fidelity

Enumerate every writer of `answer`, `final_text`, `abstain`, tool names and
arguments, workflow payloads, identifiers and execution/usage records. Trace
parsing, normalization, field omission, overflow and response assembly after
the declared final authority. Include optional fields and rare recovery paths.

W6 delegation and W8 serialization remain permitted on their exact conditions.
A field-specific overflow rule requires its published protocol version,
measurement unit, limit, measurement point and permitted outcome. No local
threshold may silently remove a model-authored field, migrate it to another
field, or change which field the scorer reads. Retaining its text elsewhere
does not cure the field change. An optional field omitted by the model is
different from an authored field deleted by the host.

Review mixed-format and boundary cases as well as normal values; test both
sides of every threshold. A helper named "format" or "fold" must be inspected
through the final response writer. A protocol/schema label is not proof that
the live contract authorizes the transformation.

## R5: execution guards precede execution

Trace the capability from the model-visible catalog through the dispatch map
and wrapper to the first local or external side effect. Visibility and
executability are separate obligations. A provider setting that discourages a
call does not enforce revocation in the executor.

Explicitly revoked calls must be blocked before delivery and represented as
blocked, including stale or malformed-provider emissions. Successful
duplicate suppression compares complete tool identity and canonical arguments
before another non-idempotent execution. Different arguments and separately
requested repetitions remain distinct and executable when authorized.
Read-only repetition, pre-delivery retry and delivery-unknown handling retain
their W11/W12 rules. A post-execution stop does not prevent a duplicate effect.

A genuine model emission does not authorize an otherwise prohibited effect.
Matching an emitted-call ledger cannot justify executing revoked calls or
repeating non-idempotent effects. Represent blocked, not-delivered,
delivery-unknown, delivered-failed and completed outcomes faithfully.

## R6: evidence-backed verdict and reuse

The [evidence specification](policy-v14-review-evidence.md) defines the required
review records and deterministic regression obligations. Every pass cites its
support; every rejection defeats applicable safe harbors. Findings identify
whether evidence is static source proof, controlled reproduction, production
observation, or statistical attribution. Illustrative witnesses must never be
reported as observed incidents.

A prior clear is evidence to inspect, not an exemption. Reused evidence binds
all v13 identities and the specific unchanged dependency/authority boundary.
A changed caller, configuration, adapter or downstream writer invalidates
reuse for that property. A repair closes only its own proven finding.

Same-artifact contradictory decisions must retain the earlier decision and
state the new evidence, changed interpretation or changed applicable policy.
Same-hotkey ancestors are separately identified and reviewed before any
action. Their automatic reappearance on a leaderboard is not authorization
for a blanket rejection.

## Outcomes and incomplete verification

Final outcomes remain CLEAR or REJECT. V14 creates no third final state.

- CLEAR requires the complete v13 mandatory verification plus R1–R6 evidence
  and the applicable regression profile; no unresolved material obligation.
- A complete causal integrity/security proof yields REJECT under the existing
  I/S code, with `violation_proven: true`. Unrelated unchecked paths are listed
  as limitations, never passed by inference.
- A mandatory verification still incomplete after the applicable published
  retry/deadline yields REJECT under V1/V2/V3 with
  `violation_proven: false` and the attributed failure domain.
- A missing reviewer-created ledger is a platform failure unless evidence
  proves a different predefined failure domain. It is not cheating.
- A checksum match, high score, prior clear, automated pass, number of
  reviewers or absence of findings does not establish clearance.

V14 inherits v13 retry/deadline treatment. Before activation, the published
release profile must select actual retry counts, independent-worker
requirements and deadlines; the base's recommended defaults alone are not an
operational configuration. Pending work remains temporary until that deadline.

## Activation and implementation boundary

This draft neither increments `SCREENING_POLICY_VERSION` nor raises an
activation ceiling, changes screening prompts, signs v14 decisions, rescreens
agents, changes benchmark scoring or modifies eligibility. V13 remains the
applicable policy until an authorized v14 activation takes effect.

Before v14 is schedulable, all v13 prerequisites and the following must have
published, tested evidence:

1. A manifest binding the pinned base, this policy, evidence specification and
   implemented verification profile; historical v13 results remain verifiable.
2. A backward-compatible evidence schema and signed digest binding supported
   by producer and consumer; stored immutable records and an audited Backroom
   read surface exposing obligation coverage and decision history.
3. Worker/Platform enforcement refusing CLEAR when required records or checks
   are missing; failure attribution, bounded retries and deadline finalization.
4. The regression profile executed with benign controls and genuine repairs,
   published false-positive/missed-violation results and capacity thresholds
   met. Public minimal fixtures supplement rather than replace private
   v13 opaque/metamorphic testing where that testing is required.
5. Fleet adoption and exact-artifact approval/eligibility behavior verified
   through ranking and weight generation, including predecessor re-entry.
6. A published transition revision specifying scope, activation time, existing
   approval treatment, rescreen order, cutoffs, convergence and rollback.
   No automatic owner-wide or ancestor-wide enforcement.
7. A separately authorized activation through Backroom. No rollout or
   grandfathering behavior may be invented during an individual review.

Implementing a trusted-prompt wire gate is separate work. If a later design
requires byte-preserving prompts instead of W2's semantic projection, that is
a substantive prospective rule change requiring its own published version.
