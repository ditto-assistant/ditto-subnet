# SN118 source-review policy v13

Status: implemented in the screener, activation pending. This policy takes
effect only through the versioned activation control after every prerequisite
at the end of this document is satisfied.

The final operator outcomes are `CLEAR` and `REJECT`. Screening pass,
quarantine, retry, and review are processing states, not final dispositions.

## Governing principles

- Every decision binds one exact submission UUID, source SHA-256, built-image
  digest, build configuration, served entrypoint, permitted runtime
  configuration, benchmark version, and adjudication-policy version.
- Findings never transfer automatically to a sibling, name, owner, coldkey,
  hotkey, or lineage. Evidence is reusable only after proving byte,
  configuration, behavior, and policy equivalence.
- Review the applicable lifecycle: archive extraction, install, build, startup,
  request handling, retrieval, inference, tools, recovery, background work,
  persistence, and shutdown.
- Scores, similarity, fingerprints, names, comments, rapid improvement, and
  history are leads, not proof.
- Declaration documents authority; it does not grant authority, authorization,
  or a safe harbor. Locally permitted components must also pass the end-to-end
  rules when composed.
- Conditionality is neither necessary nor sufficient. An always-on mechanism
  is activated on every request.
- A material violation needs no proof of intent or measured score advantage.
  Do not allege malware, fraud, deliberate evasion, or measured overfitting
  without separate evidence.
- Mandatory requirements, tests, limits, and activation boundaries must be
  published before enforcement. `CLEAR` is a bounded certification of the
  checks performed, not proof that no unknown defect exists.

## Definitions

- **Deciding authority:** the genuine model or permitted model planner that
  determines a substantive response or action, including an explicitly
  delegated computation.
- **Host processing:** logic outside that authority, including deterministic
  parsers, routers, validators, selectors, and non-authoritative predictors.
- **Applicable evidence:** information materially relevant to the requested
  decision, including corrections, qualifications, relationships, chronology,
  and genuine execution receipts.
- **Valid capability:** one permitted by authorization, availability, resource
  limits, and the public tool/protocol contract.
- **Finalized response:** the substantive result emitted by the declared final
  deciding stage.

## White checklist: permitted behavior

A safe harbor applies only when every stated condition holds. Passing one item
does not clear the artifact.

### W1: genuine model authority

- Normal substantive answers come from genuine inference or a permitted W4/W5
  delegation.
- The deciding authority receives the request and necessary evidence.
- Task-level uncertainty, refusal, and abstention remain its decisions.
- An ornamental model call does not establish authority over a separately
  generated answer.
- The finalized result remains authoritative through serialization.
- Declared protocol, authorization, and infrastructure errors may be mechanical
  when they do not masquerade as successful task answers.

### W2: retrieval and evidence preservation

- Generic lexical, vector, graph, cross-encoder, and learned retrieval are
  permitted, including bounded multistage selection before substantive
  reasoning.
- An ordinary retrieval miss is not automatically an integrity violation.
- Retrieval uses the authenticated principal's authorized data.
- Preserve explicitly supplied evidence, evidence already relied upon in
  substantive reasoning, evidence supporting or contradicting advice, and
  applicable execution receipts.
- Applicable corrections, qualifications, chronology, relationships, and
  receipts reach the deciding authority.
- Preliminary summaries remain advisory and do not replace necessary originals.
- Continuations, retries, compaction, and fallbacks preserve necessary evidence.
- Reordering and genuine deduplication preserve distinct material facts/events.
- A pointer to unavailable evidence is not a substitute for supplying it.
- Context limits alone prove neither compliance nor a violation.

### W3: optimization and capability routing

- Prompt, model, temperature, token, context-depth, and candidate-pool tuning
  are permitted.
- Ordinary domain vocabulary, typo handling, and broad capability routing are
  permitted.
- Capability restrictions may enforce applicable authorization, safety,
  privacy, configuration, availability, protocol, or published resource rules.
- Restrictions are correct for the exact request/state and respect quotations,
  entity names, negation scope, and separately requested effects.
- Ambiguity does not silently remove a plausibly applicable capability before
  genuine model/planner consideration.
- A no-tool route is allowed when retained evidence or ordinary reasoning can
  satisfy the request without bypassing a required external effect or state.
- A prediction that a tool is unnecessary is not logical impossibility.
- Resource failure is reported honestly, not as successful completion.

### W4: calculators and deterministic computation

- Generic domain calculations are permitted.
- Applicability, operands, signs, units, chronology, and scope originate from
  the request, genuine model, genuine tool contract, or an applicable
  benchmark-independent product contract.
- A calculator faithfully performs its advertised operation.
- A tool receipt has authority only within its contract and does not establish
  relevance automatically.
- Advisory calculations remain non-authoritative beside complete applicable
  evidence, and the deciding model may reject, revise, or recompute them.
- No hidden expected-answer comparison selects the final answer.
- Naming a helper `calculator` does not authorize benchmark-specific operands.

### W5: model-authored programs and delegated results

- A user or genuine model may author a temporary program over runtime evidence.
- A generic interpreter may execute that program faithfully.
- Validation covers syntax, references, types, safety, and resource bounds; it
  does not enforce a hidden expected answer or benchmark interpretation.
- A genuine model may designate the executed result as final when delegation
  fixes the operation, inputs, scope, units, and representation before
  execution.
- A second model call is unnecessary after valid semantic delegation.
- Failure does not activate a benchmark-specific answer fallback.

### W6: shape recovery, critique, and best-of-N

- The pipeline declares advisory and deciding stages plus their schemas,
  selection rules, and recovery rules.
- Shape recovery addresses missing, empty, malformed, or protocol-invalid
  output. It does not classify answer meaning, refusal, desired values, units,
  or expected tool use as syntax.
- The first structurally valid shape-recovery result ships without host semantic
  comparison.
- Substantive critique/best-of-N uses a genuine deciding model with the request,
  candidates, and necessary evidence.
- That model may preserve refusal/uncertainty or reject any suggested correction.
- Host logic does not continue until a preferred value, vocabulary, or action
  appears.
- The declared final deciding result is preserved.

### W7: mechanical serialization and overflow

- Explicit model-authored fields are copied faithfully. `ANSWER:` extraction
  copies the value expressly placed there.
- Optional fields stay absent when the model did not author them.
- A global projection is fixed, declared, public-protocol-permitted, and does
  not vary with meaning, length, uncertainty, request family, or scoring.
- A versioned field-specific allowlist may normalize wrappers, boundary
  whitespace, and equivalent typography without changing meaningful internal
  whitespace, digits, signs, words, units, directions, identifiers, secrets,
  quotations, code, or tool arguments.
- Response length limits come from the public versioned protocol, which states
  the counting unit, measurement point, and overflow behavior. Raw UTF-8 bytes
  are preferred.
- Overflow neither infers abstention nor selects a likely-scoring substring.
  A permitted length-only model reserialization receives complete evidence and
  no expected semantic answer; its first compliant result ships.
- Without a public limit, a miner threshold cannot justify deleting,
  relocating, truncating, or changing precedence of a scored field.

### W8: planner/executor architectures

- A genuine model planner may be the deciding authority when it receives the
  unchanged request, necessary evidence, and materially applicable catalog.
- Its identity, inputs, outputs, and authority are declared. It may choose no
  action or author an exact plan, which a deterministic executor may pin.
- Unexpected results and invalidated preconditions cause safe stopping or
  legitimate replanning.
- No hidden expected-answer/trajectory gate selects or repairs the plan.
- Advisory plans remain rejectable, replaceable, and reorderable; every valid
  deviation can execute and remain represented accurately.
- Planner selection never replaces authorization or safety checks.

### W9: execution and reporting fidelity

- Reported task calls originate from a genuine model/permitted planner and
  correspond to actual execution.
- Names, arguments, results, ordering, outcomes, usage, cost, and latency are
  faithful; local and external execution have distinguishable provenance.
- Side-effect retries follow the endpoint's idempotency contract.
- Duplicate suppression follows genuine success and preserves separately
  requested repetitions and unrelated pending capabilities.
- Optional self-report may be absent only when a protocol ledger is authoritative.

### W10: opaque components

- Submission-controlled opaque models/executables declare digest, load site,
  role, inputs, outputs, and downstream authority. Hosted inference records the
  supported provider/model/version provenance without inventing unavailable
  weight hashes.
- Review determines the actual role instead of trusting its label.
- Presentation-only ranking preserves the complete material evidence set.
- Each component meets the requirements in
  [policy-v13-opaque-verification.md](policy-v13-opaque-verification.md) for its
  role. Private tests are required only when that role requires them or source
  causality remains unresolved.
- An unused opaque file is not treated like an authoritative planner merely
  because it exists.
- Missing mandatory proof is a verification deficiency, not misconduct.
- Human review substitutes for a mandatory test only through a published
  equivalent-evidence route.

### W11: builds, dependencies, and dead code

- Bind source, build inputs, executable dependencies, generated code, image,
  entrypoint, and permitted configuration. Review/scoring use that image.
- Changes outside the envelope require fresh review.
- Tests, fixtures, diagnostics, and helpers are not violations by presence,
  copying, or compilation alone.
- Reachability includes extraction, build hooks, macros, imports, loaders,
  startup, background work, recovery, and shutdown.
- A caller excluded by the effective build does not establish runtime
  reachability. Lack of observed execution does not prove deadness.
- Refuting one dormant finding does not clear the rest of the artifact.
- Enabling a feature requires reassessment but does not itself prove a breach.
- Provenance establishes identity, not safety.

### W12: evaluation-independent development

- Legitimately public documentation, data, aggregates, and archived examples
  may support general development.
- Served logic does not use evaluation identities or expected outputs as
  answer/routing lookup keys.
- Private evaluation data is used only within authorization.
- Commitment precedes private challenge randomness.
- Protocol-version branches implement declared compatibility.
- Logging, endpoint use, user isolation, and provider configuration are not
  evasion without a prohibited substantive effect.
- Ordinary brittleness or performance loss is not automatically cheating.

## Black checklist: rejection conditions

A finding must establish its causal chain and defeat applicable safe harbors.

### I1: model-authority bypass

- A normal substantive answer or task-level abstention is supplied without
  genuine deciding authority.
- A model is ornamental while another component secretly supplies the answer.
- Provider failure activates a deterministic scored-answer fallback.
- A benchmark answer lookup is represented as genuine model output.

### I2: material evidence loss

- Protected applicable evidence is removed/altered before decision.
- A derived interpretation remains while necessary support or contradiction is
  removed.
- Recovery/finalization loses a material correction, qualification, record, or
  receipt.
- Expected-answer agreement or a preferred conclusion controls retention.

Proof names the exact evidence, applicability, discard location, deciding input,
material alternative affected, and reachable path. Ordinary relevance ranking
or an isolated miss is insufficient.

### I3: suppression of model decisions

- Host logic rejects/replaces a valid deciding result to obtain a preferred
  answer, unit, vocabulary, or action.
- Generation continues until uncertainty, refusal, or disagreement disappears.
- A valid final no-call decision is replaced because the host expected a call.
- A shape validator enforces hidden semantic correctness.
- A critic/selector is constrained by benchmark answers or unauthorized truth.

Genuine model-led refinement satisfying W6 is permitted.

### I4: unauthorized final-field changes

- Host processing adds, changes, removes, or replaces finalized substantive
  fields outside W5/W7.
- `unknown`, `none`, `n/a`, uncertainty, or refusal causes conditional deletion
  or host-created abstention.
- Currency is rescaled or an equivalent value is added after final authority.
- Direction words are mapped to preferred grader vocabulary.
- A missing field is synthesized from prose or host interpretation.
- A miner length threshold changes which field the scorer reads.
- Values are truncated, summarized, completed, or selectively extracted beyond
  an authorized parser.
- Model-selected tool arguments receive unauthorized substantive changes.

Preserving original words elsewhere does not cure changing their authoritative
field.

### I5: benchmark-shaped semantic processing

- Generator-, fixture-, grader-, or evaluation-specific assumptions determine
  interpretation, operands, scope, answer values, vocabulary, trajectories,
  call counts, or completion criteria.
- A family classifier activates a fixed benchmark answer/interpretation recipe.
- A distributed mechanism across routing, retrieval, prompts, calculators, and
  finalizers supplies the recipe.
- Prompt prose performs prohibited compilation despite a model writing the
  final sentence.
- A model-authored program is accepted only when it matches host-expected
  semantics.
- Evaluator-specific assumptions replace legitimate completion judgment.

Proof identifies the benchmark-specific assumption, its activation (including
unconditional application), its substantive effect, and why normal product or
domain behavior does not explain it. Common formulas, finite vocabularies, typo
handling, ordinary product rules, prescriptive prose, and fixture references
alone are insufficient.

### I6: execution or measurement misrepresentation

- Calls, receipts, results, success, or measurements are fabricated.
- A proposal is reported as execution.
- Names, arguments, order, results, usage, cost, or latency are misreported.
- Calls/receipts are replayed across requests/users outside the contract.
- Local work is falsely represented as external execution.
- Genuine execution is hidden to affect grading.
- Failed, unauthorized, or unexecuted work is reported as successful.

### I7: unauthorized capability or planning control

- Host processing removes an applicable authorized available capability without
  a valid W3 reason.
- Quoted titles, entity names, declaration wording, or incorrectly scoped
  negation remove a required capability.
- A host rule or benchmark-fitted predictor selects an exact trajectory without
  permitted deciding authority.
- A valid model/planner deviation is suppressed.
- Duplicate/success handling removes unrelated requested capabilities.
- Host completion state prevents another requested effect.
- Planner authority is concealed or falsely described as advisory.

Proof names the triggering request/state, catalog, authority source, removed or
enforced choice, valid blocked alternative, and consequence. Authorized staged
execution under W8 is permitted; prescriptive language alone is insufficient.

### I8: prohibited evaluation dependence

- Evaluation identity changes substantive retrieval, authority, capabilities,
  execution, or output.
- Active case IDs, seeds, expected answers, dataset hashes, or evaluation
  markers become runtime answer/routing keys.
- Audit detection enables compliant behavior while scored traffic uses a
  prohibited path.
- Unauthorized private evaluation information trains/selects the served system.
- Controlled behavioral evidence satisfies the published attribution standard.

Complete source proof is independently enforceable. Statistical enforcement
requires the published behavioral system to be operational.

### S1: unauthorized data access

- Data or sensitive metadata crosses the authenticated principal's authorized
  scope through retrieval, caches, jobs, logs, shared state, or retries.
- Caller-supplied identifiers are accepted as authorization.
- Delayed execution ignores changed/revoked authorization.

Explicit authorized sharing and administration are permitted.

### S2: unauthorized execution, disclosure, or persistence

- Reachable lifecycle code performs unauthorized credential access/disclosure,
  filesystem modification, command execution, executable deserialization,
  privilege escalation, or persistence.
- Privileged traffic/credentials are redirected outside authorized destinations.
- Undeclared executable substitution changes the reviewed agent.
- Untrusted source, tool, or model content gains execution authority.

No model, score benefit, malicious intent, or real-secret leakage is required.

### S3: screening or audit evasion

- The artifact detects review/evaluation conditions to conceal independently
  prohibited behavior.
- Reviewed and scored implementations differ outside the approved envelope.
- Hidden flags or entrypoints disable prohibited behavior during inspection.

Ordinary changes in data, catalogs, initialization, or authorized providers are
insufficient.

### S4: resource or isolation violations

- Work escapes published resource, process, network, concurrency, or
  cancellation boundaries.
- A submission interferes materially with other tenants/builds/requests.
- Processes persist beyond their authorized lifecycle.
- Isolation/resource controls are bypassed or materially defeated.

Slow work within its allocation is ordinarily a performance issue.

### Q1: material mandatory-contract failure

- The artifact violates a published mandatory build, startup, protocol,
  authorization, persistence, or capability contract.
- The defect meets predeclared materiality criteria.
- Evidence attributes it to the artifact, not platform/provider failure.

Ordinary model mistakes, cosmetic defects, and harmless dead code are
insufficient without an applicable mandatory rule.

### V1: required evidence missing

- A predefined applicable evidence requirement exists.
- The exact missing item is identified.
- The published submission/correction process was followed.
- No accepted equivalent evidence satisfies it.

This is a certification failure, not proof of misconduct.

### V2: verification not completed

- A predefined mandatory verification remains incomplete after published
  retries and deadlines.
- The missing verification and failure domain are recorded.
- Platform/provider failure is not represented as artifact misconduct.
- Suspicion is not converted into an unpublished requirement.

V1/V2-only rejection records `violation_proven: false`.

## Evidence standards

Integrity and contract findings prove:

```text
valid triggering input or state
-> reachable mechanism
-> authority, evidence, field, or capability change
-> substantive response or execution consequence
```

Security findings prove:

```text
reachable lifecycle trigger
-> operation
-> protected resource or authorization boundary
-> unauthorized effect or material availability impact
```

Every decision cites executable locations/effective build conditions, inspects
callers/wrappers/recovery/final writers, addresses safe harbors, separates
source proof from reproduction/production observation/hypothesis, records
missing or opaque evidence, and never reports an illustrative witness as an
observed incident.

## Behavioral verification

When required by a published role standard or unresolved source causality:

- Commit the artifact before private challenge randomness.
- Version and content-address the challenge manifest.
- Predeclare sample sizes, material effects, confidence criteria, replication,
  and multiple-comparison handling.
- Use paired controls through equivalent infrastructure.
- Preserve task meaning, public API contracts, identities, relationships,
  chronology, units, and authorization.
- Use independent hidden rotations and fresh task compositions.
- Account for model randomness and infrastructure failures.
- Establish attribution before calling degradation evaluation dependence.
- Include known violations, genuine repairs, and benign implementations, and
  measure incorrect decisions plus reviewer disagreement.

Reviewer count, token expenditure, and absence of tool errors are not evidence
that the policy is accurate.

## Final outcomes

### CLEAR

- Exact artifact/configuration identity is verified.
- Every predefined applicable mandatory check is complete.
- Material leads were evaluated against the published proof standard.
- No rejection condition is established.
- Required evidence, scope, and limitations are recorded.

### REJECT

- At least one I, S, Q, V1, or V2 condition is established under its evidence
  standard.
- The decision records exact reason codes and distinguishes proven violations
  from certification failures.
- Scores, artifacts, evidence, and history are preserved.
- No owner/hotkey/sibling/lineage penalty follows automatically.
- Appeals produce a new recorded `CLEAR` or `REJECT` without erasing history.

Queued, reviewing, and retrying are internal processing states, not additional
final outcomes.

## Approval and emission eligibility

- At each weight cutoff, positive-emission eligibility requires a valid
  qualifying `CLEAR` for the exact selected artifact under the applicable
  policy or published transition rules.
- `CLEAR` establishes eligibility; ranking/reward rules determine allocation.
- Another artifact's approval grants no authority.
- Processing states neither grant approval nor automatically revoke an existing
  valid approval.
- Artifacts without valid approval are excluded from new positive-weight
  selection.
- Expiry, suspension, supersession, revocation, effective cutoffs, and already
  committed exposure are recorded until validator/chain convergence.
- No indefinite override substitutes for completed clearance.

## Activation prerequisites

- Publish the opaque-component role-verification specification and accepted
  equivalent-evidence routes.
- Make every mandatory verification route for the activated scope operational.
- Publish and meet review-capacity, completion, latency, and backlog thresholds.
- Publish retry deadlines and platform/provider failure treatment.
- Publish transition, approval-expiry, suspension, revocation, weight-cutoff,
  convergence, and rollback rules.
- Verify the exact-artifact approval gate through ranking and weight generation.
- A staged activation names the exact rules/tests/artifact classes it covers.
- Scheduled/unavailable verification is not an implemented safeguard.
- Rollback preserves evidence/revocations and does not restore fail-open approval.

## Required decision record

```text
outcome: CLEAR | REJECT
reason_codes: [...]
violation_proven: true | false
submission_uuid:
artifact_sha256:
image_digest:
build_configuration:
served_entrypoint:
permitted_runtime_configuration:
benchmark_version:
screening_policy_version:
applied_adjudication_policy_version:
policy_digest:
verification_specification_version:
review_scope:
completed_checks:
evidence_references:
failure_domain: artifact | platform | provider | none
reviewer:
decided_at:
supersedes_decision:
approval_effect:
effective_cutoff:
```

A fix resolves only the exact findings it demonstrably repairs. Review every
remaining obligation independently.
