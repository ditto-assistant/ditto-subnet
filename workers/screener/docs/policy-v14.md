# SN118 source-review policy v14

Status: published with screener support; activation pending. Policy v14 is the
complete policy v13 text plus the additive [Policy v14 addendum](#policy-v14-addendum)
at the end of this document. It changes no v13 invariant, safe harbor, outcome,
proof standard, or reason code; it adds evidence fields, one operational safe
harbor, three additive reason codes, one submission declaration, and the
co-activation gate. This policy takes effect only through the versioned
activation control, scheduled together with DittoBench v13 with
`rescreen_scored=true`, after every prerequisite and the
[co-activation gate](#co-activation-gate) at the end of this document are
satisfied. Policy v13 is never activated standalone.

Policy v14 is a strict two-outcome policy. Every completed review ends in
`CLEAR` or `REJECT`. When verification cannot be completed the result is
`REJECT` with `violation_proven: false` and an explicit failure domain; such a
rejection is a certification failure and must never be described as cheating,
fraud, or deliberate evasion.

## Final outcomes

Policy v14 has exactly two final outcomes:

```text
CLEAR
REJECT
```

Queued, building, screening, retrying, and adjudicating are temporary
processing states. They must resolve to `CLEAR` or `REJECT` by the published
deadline.

There is no `REVIEW_INCOMPLETE`, `INCONCLUSIVE`, implied clearance, or
indefinite hold outcome.

## Exact-artifact scope

Every decision binds to the exact:

- submission UUID;
- artifact SHA-256;
- built-image digest;
- build configuration;
- served entrypoint;
- permitted runtime configuration;
- benchmark version;
- policy version and digest;
- opaque-component manifest; and
- verification-profile digest.

Findings never transfer automatically across versions, siblings, names,
owners, coldkeys, hotkeys, or lineages.

Earlier evidence may be reused only when all bound identities and relevant
configuration are identical.

## Activation

- Policy v14 takes effect only through a published activation revision
  scheduled together with DittoBench v13 and `rescreen_scored=true`.
- The activation record contains the exact policy digest and activation time.
- Existing v12 and v13 results remain historical results under the policy
  version that produced them.
- Applying v14 to an existing artifact requires an explicit v14 rescreen.
- `rescreen_scored=true` applies identical criteria to every selected
  artifact.
- Earlier scores, evidence, artifacts, and decisions remain preserved.
- A later decision supersedes eligibility without erasing history.

## Governing principles

- Every decision binds one exact submission UUID, source SHA-256, built-image
  digest, build configuration, served entrypoint, permitted runtime
  configuration, benchmark version, and adjudication-policy version.
- Findings never transfer automatically to a sibling, name, owner, coldkey,
  hotkey, or lineage. Evidence is reusable only after proving byte,
  configuration, behavior, and policy equivalence.
- Review the applicable lifecycle: archive extraction, install, build,
  startup, request handling, retrieval, inference, tools, recovery,
  background work, persistence, and shutdown.
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

- Normal substantive answers originate from genuine model inference, or from a
  permitted W5/W6 delegation.
- Refusal, uncertainty, and substantive abstention remain model decisions.
- The model receives the request and evidence needed to decide.
- A model call is not ornamental. An ornamental call does not establish
  authority over a separately generated answer.
- The final deciding result remains authoritative through serialization.
- Mechanical protocol, infrastructure, and authorization errors do not
  masquerade as successful answers. Declared errors of those kinds may be
  mechanical.
- Provider failure does not activate a deterministic answer engine.

### W2: trusted instruction retention

Every deciding answer-producing call receives:

- the original user request;
- applicable trusted host-system instructions;
- safety and authorization constraints;
- user-scope requirements;
- applicable date and temporal context;
- output requirements; and
- relevant execution restrictions.

Prompt projection is allowed only when it preserves all applicable meaning and
restrictions.

A summary or digest is not sufficient when the deciding model needs the
original instruction.

### W3: retrieval and final evidence

Allowed retrieval includes BM25, lexical search, vector search, graph
retrieval, cross-encoder reranking, learned reranking, lexical-first semantic
fallback, semantic-first lexical fallback, bounded top-k selection, and
multistage candidate selection before substantive reasoning.

Policy v13 does not require perfect recall. Retrieval uses the authenticated
principal's authorized data.

An intermediate retrieval candidate is not protected evidence merely because
one stage scored it. Protection begins when the pipeline:

- designates the record as part of the final evidence set;
- attaches it to a deciding transcript;
- relies on it for substantive reasoning;
- uses it to create an advisory value; or
- uses it to select a tool argument.

Once protected:

- corrections remain available;
- qualifications remain available;
- contradictory evidence remains available;
- assistant-side evidence remains available when applicable;
- completed tool arguments and receipts remain available;
- applicable chronology and relationships reach the deciding authority;
- retries preserve it;
- critique preserves it;
- compaction preserves it; and
- fallback preserves it.

Additional retention rules:

- Preliminary summaries remain advisory and do not replace necessary
  originals.
- Reordering and genuine deduplication preserve distinct material facts and
  events.
- A pointer to unavailable evidence is not a substitute for supplying it.
- Context limits alone prove neither compliance nor a violation.

An ordinary retrieval miss is a score-quality issue and is not automatically
an integrity violation.

### W4: routing and optimization

- Prompt, model, temperature, reasoning effort, context size, token,
  context-depth, and candidate-pool tuning are allowed.
- Product-surface routing is allowed.
- Ordinary domain vocabulary, typo handling, and broad capability routing are
  allowed.
- Authorization, safety, privacy, configuration, availability, protocol, and
  published resource limits may restrict capability classes.
- Explicit no-tool instructions may restrict tools.
- Restrictions are correct for the exact request and state, and respect
  quotations, entity names, negation scope, and separately requested effects.
- A classifier may reorder a complete catalog.
- A classifier may widen an uncertain request into an action class.
- Ambiguous requests that plausibly match an available tool are deferred to a
  genuine model or planner; ambiguity does not silently remove a plausibly
  applicable capability first.
- A no-tool route is allowed when retained evidence or ordinary reasoning can
  satisfy the request without bypassing a required external effect or state.
- Low confidence alone does not justify removing every plausibly applicable
  capability. A prediction that a tool is unnecessary is not logical
  impossibility.
- Resource failure is reported honestly, not as successful completion.
- Poor routing that does not displace model authority remains a performance
  issue.

### W5: financial formulas and calculators

Financial, statistical, temporal, scientific, and conversion calculations are
allowed when:

- the operation is described through a visible runtime contract;
- the model chooses whether to use it;
- the model chooses the result type;
- the model selects every operand;
- the model selects categories, signs, scope, currency, units, and period;
- applicability, operands, signs, units, chronology, and scope originate from
  the request, a genuine model, a genuine tool contract, or an applicable
  benchmark-independent product contract;
- the host does not extract authoritative operands from narrative text;
- the calculator performs only the advertised arithmetic;
- the result is labelled as a receipt for supplied inputs;
- complete applicable evidence remains available;
- the final model may reject, revise, or recompute the result;
- no expected-value gate prefers agreement and no hidden expected-answer
  comparison selects the final answer; and
- the host does not rewrite scorer-visible output.

Allowed example:

```text
opening_balance
+ sum(budget_adjustments)
- sum(expenses)
+ sum(credits)
```

The formula itself is not a violation. It becomes prohibited when the host
selects its operands, maps benchmark narrative roles into its fields, or
publishes the result as authoritative without model judgment.

A tool receipt has authority only within its contract and does not establish
relevance automatically. Naming a helper `calculator` does not authorize
benchmark-specific operands.

### W6: model-authored programs

- A user or genuine model may author a temporary program over runtime
  evidence.
- The interpreter is generic and resource-bounded, and executes the program
  faithfully.
- Validation covers syntax, types, references, safety, and resource bounds.
- Validation does not compare against a hidden expected answer or enforce a
  benchmark interpretation.
- The model establishes the operation, inputs, scope, units, and
  representation before execution.
- The model may explicitly delegate final authority to the executed result.
  A second model call is unnecessary after valid semantic delegation.
- Advisory results remain rejectable by a later deciding model.
- Failure does not activate a benchmark-specific solver or answer fallback.

### W7: dissent, critique, and recovery

- The pipeline declares advisory and deciding stages plus their schemas,
  selection rules, and recovery rules.
- Missing, empty, malformed, or schema-invalid output may trigger recovery.
- A valid response is not rejected because its meaning is undesirable.
- Refusal, uncertainty, disagreement, units, vocabulary, or no-call behavior
  are not shape failures, and recovery does not classify them as syntax.
- A declared genuine critic may produce the final answer.
- The critic receives the original request, applicable evidence, and relevant
  drafts.
- The critic may retain refusal or uncertainty, and may reject any suggested
  correction.
- The first valid designated final result ships without host semantic
  selection or comparison.
- Best-of-N semantic selection is performed by a declared genuine deciding
  model.
- Host logic does not continue until a preferred value, vocabulary, or action
  appears.
- The declared final deciding result is preserved.

### W8: serialization and overflow

- Model-authored fields are copied faithfully.
- `ANSWER:` extraction copies only the value the model placed there.
- Optional fields remain absent unless model-authored.
- Request-independent wrapper, boundary-whitespace, and published Unicode
  normalization are allowed. A global projection is fixed, declared,
  public-protocol-permitted, and does not vary with meaning, length,
  uncertainty, request family, or scoring.
- A versioned field-specific allowlist may normalize wrappers, boundary
  whitespace, and equivalent typography without changing meaningful internal
  whitespace, digits, signs, words, units, directions, identifiers, secrets,
  quotations, code, or tool arguments.
- Digits, signs, words, units, identifiers, and meaningful internal whitespace
  remain unchanged.
- Direction words are not mapped to grader vocabulary.
- Financial values are not rescaled after finalization.
- Abstention is not synthesized from prose.

Length handling is allowed only when a public protocol defines the limit, the
counting unit, the measurement point, the affected field, and the overflow
behavior. Raw UTF-8 bytes are the preferred counting unit.

Overflow neither infers abstention nor selects a likely-scoring substring. A
permitted length-only model reserialization receives complete evidence and no
expected semantic answer; its first compliant result ships.

A miner-defined threshold cannot delete or move a scorer-visible field, or
change the precedence of one.

### W9: planner authority

A model planner may be authoritative when:

- its role is declared, along with its identity, inputs, outputs, and
  authority;
- it receives the complete, unchanged request;
- it receives applicable evidence;
- it receives the complete materially applicable catalog;
- it may select no action;
- it authors the exact plan and arguments;
- no hidden expected-answer or expected-trajectory gate accepts or repairs its
  plan;
- the executor follows the plan faithfully; and
- unexpected results and invalidated preconditions permit safe stopping or
  genuine replanning.

A deterministic executor may pin the next dependency step only after such an
authoritative plan.

An advisory planner remains skippable, replaceable, addable, and reorderable
by the later deciding model, and every valid deviation can execute and remain
represented accurately.

Planner selection never replaces authorization or safety checks.

### W10: catalog fidelity

- Request-supplied names remain unchanged.
- Request-supplied descriptions remain authoritative.
- Required fields, schemas, enums, and constants remain unchanged.
- Miner guidance is presented separately as advisory material.
- Compatibility rewriting requires a published protocol version or
  host-provided mapping.
- Partial, mixed, custom, or unknown catalogs remain unchanged.
- A natural singleton arises only from real authorization, availability, or
  the supplied catalog.
- A semantic guess does not manufacture a singleton.
- Catalog reordering preserves every applicable capability.

### W11: tool stopping and repetition

- Tool removal revokes presentation and execution authority.
- A call emitted after explicit revocation does not execute.
- Blocked calls are recorded as blocked.
- Repetition detection occurs before another non-idempotent execution.
- Exact successful duplicates may be suppressed, and suppression follows
  genuine success.
- Duplicate identity includes the complete tool name and canonical arguments.
- Different arguments remain distinct.
- Separately requested repetitions remain executable, and unrelated pending
  capabilities are preserved.
- Pre-delivery failures may retry.
- Delivery-unknown side effects retry only under an idempotency contract, and
  side-effect retries follow the endpoint's idempotency contract.

### W12: execution and usage reporting

Every execution receives one state:

```text
blocked_before_execution
not_delivered
delivery_unknown
delivered_failed
completed
```

- Every completed reported call was genuinely selected by a genuine model or
  permitted planner and genuinely executed.
- Every call known to cross the execution boundary appears in the
  authoritative ledger.
- Names, arguments, results, order, outcomes, usage, cost, and latency remain
  faithful.
- Local and external execution have distinct, distinguishable provenance.
- Proposals are not reported as execution.
- Optional self-report may be absent only when a protocol ledger is
  authoritative.
- Token usage includes planners, retries, critics, reviews, recovery, and
  finalization.
- An uncertain outcome is never presented as success.

### W13: opaque components

Each opaque executable component declares its path, complete digest, detected
format, load site, runtime role, inputs, outputs, downstream authority,
training-data category, training-data provenance, private-evaluation-data
declaration, and deterministic or stochastic behavior. Hosted inference
records the supported provider, model, and version provenance without
inventing unavailable weight hashes.

- The complete artifact SHA covers every component.
- Review caches include the artifact SHA and policy digest.
- Changing one byte invalidates cached artifact review.
- Bounded binary analysis records whether hashing and structural analysis were
  complete.
- Review determines the actual role instead of trusting its label.
- A generic opaque reranker may rank current-user records.
- Presentation-only ranking preserves the complete material evidence set.
- A classifier may reorder a complete catalog.
- Changed weights alone do not prove misconduct.
- An unused opaque file is not treated like an authoritative planner merely
  because it exists.
- Opaque components with material authority complete the private behavioral
  suite.
- Missing mandatory proof is a verification deficiency, not misconduct.
- Human review substitutes for a mandatory test only through a published
  equivalent-evidence route.

Each component meets the requirements in
[policy-v13-opaque-verification.md](policy-v13-opaque-verification.md) for its
role. Private tests are required only when that role requires them or source
causality remains unresolved.

Missing mandatory opaque-component evidence results in `REJECT` after the
published retry and remediation procedure.

### W14: build and runtime integrity

- Archive, dependencies, build inputs, executable dependencies, generated
  code, image, and entrypoint are bound, along with permitted configuration.
- Review and scoring use the approved image.
- Mutable dependencies cannot replace the reviewed runtime.
- Changes outside the envelope require fresh review.
- Tests, fixtures, diagnostics, and helpers are not violations by presence,
  copying, or compilation alone, and require production reachability before
  they support a finding.
- Docker `COPY` alone does not establish execution.
- Reachability includes extraction, build hooks, macros, imports, loaders,
  startup, background work, retries, fallbacks, recovery, and shutdown.
- A caller excluded by the effective build does not establish runtime
  reachability. Lack of observed execution does not prove deadness.
- Refuting one dormant finding does not clear the rest of the artifact.
- Enabling a feature requires reassessment but does not itself prove a breach.
- Provenance establishes identity, not safety.
- Material runtime changes require another review.

### W15: user isolation and security

- `/seed` and `/run` resolve the same authenticated principal.
- Retrieval, graph, cache, model, tool, response, and logging paths preserve
  user identity.
- A documented default user represents one default principal.
- Different authenticated users never share substantive content.
- Credentials remain outside submission-controlled output.
- External requests use authorized endpoints.
- Logging, endpoint use, user isolation, and provider configuration are not
  evasion without a prohibited substantive effect.

### W16: evaluation independence

- Artifact commitment precedes private challenge generation and private
  challenge randomness.
- Active private seeds, expected outputs, and case identities remain hidden.
- Active per-case results cannot influence a weight-eligible artifact.
- Served behavior does not change according to evaluation identity, and served
  logic does not use evaluation identities or expected outputs as answer or
  routing lookup keys.
- Legitimately public documentation, data, aggregates, retired examples, and
  archived examples may support general development.
- Private evaluation data is used only within authorization.
- Protocol-version branches implement published, declared compatibility.
- Behavior generalizes across renamed fields, paraphrases, reordered records,
  rotated entities, and reordered catalogs.
- Active private evidence remains embargoed until it can no longer influence
  eligibility.
- Ordinary brittleness or performance loss is not automatically cheating.

## Black checklist: rejection conditions

A finding must establish its causal chain and defeat applicable safe harbors.

### I1: model-authority bypass

Reject when a normal answer or abstention is supplied before or instead of
genuine deciding authority.

- A normal substantive answer or task-level abstention is supplied without
  genuine deciding authority.
- A model is ornamental while another component secretly supplies the answer.
- Provider failure activates a deterministic scored-answer fallback.
- A benchmark answer lookup is represented as genuine model output.

### I2: instruction or evidence loss

Reject when applicable trusted instructions or protected final evidence are
removed before a deciding, retry, critic, or fallback model.

- Protected applicable evidence or an applicable trusted instruction is
  removed or altered before decision.
- A derived interpretation remains while necessary support or contradiction is
  removed.
- Recovery or finalization loses a material correction, qualification, record,
  or receipt.
- Expected-answer agreement or a preferred conclusion controls retention.

Required proof:

1. exact UUID and SHA;
2. exact instruction, record, argument, or receipt;
3. why it is applicable;
4. where it entered the final evidence set;
5. where it was removed;
6. the deciding input lacking it; and
7. the material alternative that can no longer be evaluated.

The proof also names the reachable path. Ordinary relevance ranking, an
isolated miss, or a hypothetical retrieval miss is insufficient. A controlled
exact-artifact reproduction is sufficient.

### I3: model-dissent suppression

Reject when host processing semantically chooses, retries, replaces, or
suppresses valid model output to obtain a preferred answer, call, vocabulary,
certainty, or refusal state.

- Host logic rejects or replaces a valid deciding result to obtain a preferred
  answer, unit, vocabulary, or action.
- Generation continues until uncertainty, refusal, or disagreement disappears.
- A valid final no-call decision is replaced because the host expected a call.
- A shape validator enforces hidden semantic correctness.
- A critic or selector is constrained by benchmark answers or unauthorized
  truth.

Genuine model-led refinement satisfying W7 is permitted.

### I4: unauthorized authority transfer

Reject when host processing changes a finalized `answer`, `final_text`,
abstention field, tool argument, workflow payload, identifier, currency value,
direction, or other scorer-visible value.

- Host processing adds, changes, removes, or replaces finalized substantive
  fields outside W6 and W8.
- `unknown`, `none`, `n/a`, uncertainty, or refusal causes conditional
  deletion or host-created abstention.
- Currency is rescaled or an equivalent value is added after final authority.
- Direction words are mapped to preferred grader vocabulary.
- A missing field is synthesized from prose or host interpretation.
- A miner length threshold changes which field the scorer reads.
- Values are truncated, summarized, completed, or selectively extracted beyond
  an authorized parser.
- Model-selected tool arguments receive unauthorized substantive changes.

Preserving original words elsewhere does not cure changing their authoritative
field.

### I5: benchmark-shaped semantic compiler

Reject when a benchmark-shaped phrase table, prompt, worksheet, formula
router, distributed compiler, or expected-answer rule determines substantive
interpretation or output.

- Generator-, fixture-, grader-, or evaluation-specific assumptions determine
  interpretation, operands, scope, answer values, vocabulary, trajectories,
  call counts, or completion criteria.
- A family classifier activates a fixed benchmark answer or interpretation
  recipe.
- A distributed mechanism across routing, retrieval, prompts, calculators, and
  finalizers supplies the recipe.
- Prompt prose performs prohibited compilation despite a model writing the
  final sentence.
- A model-authored program is accepted only when it matches host-expected
  semantics.
- Evaluator-specific assumptions replace legitimate completion judgment.

Proof identifies the benchmark-specific assumption, its activation (including
unconditional application), its substantive effect, and why normal product or
domain behavior does not explain it. Common formulas, general product
instructions, ordinary finite vocabularies, typo handling, ordinary product
rules, prescriptive prose, and fixture references alone are insufficient
unless they create a proven benchmark-specific procedure.

### I6: execution misrepresentation

Reject fabricated, hidden, replayed, materially rewritten, falsely successful,
or falsely attributed execution.

- Calls, receipts, results, success, or measurements are fabricated.
- A proposal is reported as execution.
- Names, arguments, order, results, usage, cost, or latency are misreported.
- Calls or receipts are replayed across requests or users outside the
  contract.
- Local work is falsely represented as external execution.
- Genuine execution is hidden to affect grading.
- Failed, unauthorized, or unexecuted work is reported as successful.

### I7: capability suppression or planning takeover

Reject when:

- an applicable authorized available capability is removed without a valid W4
  policy ground;
- quoted titles, entity names, declaration wording, or incorrectly scoped
  negation remove a required capability;
- a low-confidence outer router removes every plausibly applicable capability;
- a host rule or benchmark-fitted learned head selects the exact executable
  trajectory without permitted deciding authority;
- a catalog is replaced with an advisory plan;
- an unauthorized singleton is manufactured;
- a valid model or planner deviation is suppressed and cannot execute;
- distinct arguments are treated as duplicates;
- duplicate or success handling removes unrelated requested capabilities;
- host completion state prevents another requested effect;
- execution remains enabled after revocation;
- planner authority is concealed or falsely described as advisory; or
- strong fixed prompting effectively forces one trajectory.

Proof names the triggering request or state, the catalog, the authority
source, the removed or enforced choice, the valid blocked alternative, and the
consequence. Authorized staged execution under W9 is permitted; prescriptive
language alone is insufficient.

### I8: prohibited evaluation dependence

Reject when active evaluation identities, seeds, results, expected answers,
private cases, grader behavior, or audit detection materially change
retrieval, model authority, tools, execution, or scorer-visible output.

- Evaluation identity changes substantive retrieval, authority, capabilities,
  execution, or output.
- Active case IDs, seeds, expected answers, dataset hashes, or evaluation
  markers become runtime answer/routing keys.
- `bench_version` or another protocol-version field activates substantive
  routing, retrieval, capability ordering, authority, or output behavior not
  required by the published compatibility contract.
- Audit detection enables compliant behavior while scored traffic uses a
  prohibited path.
- Unauthorized private evaluation information trains/selects the served
  system.
- Controlled behavioral evidence satisfies the published attribution standard.

A public compatibility branch alone is insufficient. Complete source proof is
independently enforceable. Statistical enforcement requires the published
behavioral system to be operational.

### S1: cross-user access

Reject when another user's substantive data reaches retrieval, model context,
tools, output, logs, caches, or an external sink.

- Data or sensitive metadata crosses the authenticated principal's authorized
  scope through retrieval, caches, jobs, logs, shared state, or retries.
- Caller-supplied identifiers are accepted as authorization.
- Delayed execution ignores changed/revoked authorization.

Explicit authorized sharing and administration are permitted.

### S2: credential or data exfiltration

Reject unauthorized credential access, secret emission, private-data
exfiltration, host-resource access, or hidden runtime replacement.

A declared dependency, ordinary authorized provider call, or inert asset is not
sufficient. Prove the reachable operation and unauthorized effect.

### S3: screening evasion

Reject behavior that detects screening and activates compliant behavior only
while being reviewed.

- The artifact detects review/evaluation conditions to conceal independently
  prohibited behavior.
- Reviewed and scored implementations differ outside the approved envelope.
- Hidden flags or entrypoints disable prohibited behavior during inspection.

Ordinary changes in data, catalogs, initialization, or authorized providers
are insufficient.

### Q1: material mandatory-contract failure

- The artifact violates a published mandatory build, startup, protocol,
  authorization, persistence, or capability contract.
- The defect meets predeclared materiality criteria.
- Evidence attributes it to the artifact, not platform/provider failure.

Ordinary model mistakes, cosmetic defects, and harmless dead code are
insufficient without an applicable mandatory rule.

Findings are recorded under the `mandatory_contract_failure` review category.

### V1: required submission evidence missing

- A predefined applicable evidence requirement exists.
- The exact missing item is identified.
- The published submission/correction process was followed.
- No accepted equivalent evidence satisfies it.

This is a certification failure, not proof of misconduct.

### V2: platform verification not completed

- A predefined mandatory verification remains incomplete after the published
  retries and deadline because the platform could not complete it.
- The missing verification and failure domain are recorded.
- Platform failure is not represented as artifact misconduct.
- Suspicion is not converted into an unpublished requirement.

### V3: provider verification not completed

- A provider failure prevents mandatory verification after the published retry
  procedure.
- The missing verification and failure domain are recorded.
- Provider failure is not represented as artifact misconduct.

V1, V2, and V3 rejections record `violation_proven: false`.

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

## Mandatory verification

Every artifact must complete:

1. archive and SHA verification;
2. build and image-digest verification;
3. `/health`;
4. ordinary model-authority `/run`;
5. tool-selection `/run`;
6. `/seed` followed by memory `/run`;
7. two-user isolation;
8. host-system instruction retention;
9. tool revocation;
10. successful-duplicate suppression;
11. same-tool/different-argument execution;
12. catalog fidelity and reordering;
13. timeout and delivery-unknown reporting;
14. fallback evidence retention;
15. response-field and long-answer behavior;
16. refusal and uncertainty preservation;
17. complete token accounting;
18. opaque-component inventory; and
19. I1-I8 and S1-S3 review.

For I5, I7, I8, or materially authoritative opaque components, private
metamorphic testing is mandatory.

Minimum profile:

- 60 paired cases;
- three transformation classes;
- at least 20 cases per class;
- two independent hidden seeds;
- field/entity renaming;
- request paraphrasing;
- record reordering and decoys;
- catalog reordering and tool aliasing where applicable;
- predeclared statistical analysis;
- multiple-comparison correction;
- material degradation of at least 15 percentage points;
- 95% confidence lower bound above 5 percentage points;
- clean-control degradation no greater than 5 percentage points; and
- replication in the same direction across both seeds.

### Behavioral verification conduct

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

## Two-outcome decision rules

### CLEAR

`CLEAR` requires:

- every mandatory verification completed;
- exact artifact and image identity confirmed;
- all material leads evaluated against the published proof standard;
- explicit I1-I8 and S1-S3 decisions;
- required opaque declarations available;
- no established rejection ground; and
- complete decision evidence, including scope and limitations.

A behavioral-oracle pass, automated clear label, or operator release alone is
insufficient.

### REJECT

`REJECT` applies when any of the following is true.

#### A. Proven integrity or security violation

```text
violation_proven: true
failure_domain: artifact
```

At least one I1-I8 or S1-S3 mechanism is causally established.

#### B. Submission-controlled verification failure

```text
violation_proven: false
failure_domain: submission
```

Examples: a mandatory declaration is omitted; the archive cannot be verified; a
required endpoint contract is missing; required opaque-component evidence
remains absent; or the submitted image cannot complete mandatory checks for
artifact-controlled reasons.

#### C. Platform verification failure

```text
violation_proven: false
failure_domain: platform
```

The platform fails to complete mandatory verification after the published
retry budget and deadline.

This rejection does not allege a policy violation, does not establish
malicious intent, cannot be cited against siblings, owners, or hotkeys,
receives priority rescreen after platform recovery, and may be superseded by a
later `CLEAR`.

#### D. Provider verification failure

```text
violation_proven: false
failure_domain: provider
```

Provider failure prevents mandatory verification after the published retry
procedure. It has the same evidentiary limitations as a platform-failure
rejection.

### Non-decisive results

A review that reaches no decision is not a clearance. None of the following
may produce `CLEAR`, and none of them may submit a passing verdict:

- `source-review-inconclusive` — bounded source review exhausted without a
  decisive finding;
- `source-review-invalid-risk` — the reviewer returned an unusable risk level;
- `source-review-inconsistent-verdict` — the reviewer contradicted itself;
- `adjudicated-source-review-escalate` — adjudication ended without a
  finalized, verified finding;
- `behavioral-oracle-inconclusive` and `challenge-inconclusive` — a required
  behavioral check produced no usable observation; and
- `source-review-unavailable` — review infrastructure was unreachable.

Each resolves through the retry and deadline procedure and then terminates as:

- `REJECT` / `V1` / `failure_domain: submission`, when the artifact itself
  prevents a decision;
- `REJECT` / `V2` / `failure_domain: platform`, when the screener or platform
  could not complete the check; or
- `REJECT` / `V3` / `failure_domain: provider`, when an upstream provider
  could not complete the check.

An adjudicator that fails without a finding records only that adjudication did
not complete. It is neither clearance evidence nor rejection evidence, and it
may never be reported as cheating.

A behavioral-oracle pass obtained after a mandatory check went undecided does
not substitute for that check.

All `REJECT` outcomes remove current rank and emission eligibility while
preserving scores, artifacts, evidence, and history. The decision records
exact reason codes and distinguishes proven violations from verification
failures. No owner, hotkey, sibling, or lineage penalty follows automatically.
Appeals produce a new recorded `CLEAR` or `REJECT` without erasing history.

## Retry and deadline procedure

Before a verification-failure `REJECT`:

- attempt the published number of retries;
- use at least one independent worker when available;
- distinguish artifact, platform, and provider failures;
- preserve bounded diagnostics;
- verify the artifact SHA remains unchanged;
- apply the published deadline;
- record every attempt; and
- avoid duplicate paid work when a valid artifact-bound result already exists.

Recommended defaults:

```text
artifact-controlled failure retries: 1
provider failure retries: 2
platform failure retries: 2
independent worker required for platform/provider failure: yes
maximum verification window: 24 hours
```

At the end of the window, the result must be `CLEAR` or `REJECT`.

## Required reason codes

```text
I1.model_authority_bypass

I2.trusted_instruction_removed
I2.final_evidence_removed
I2.receipt_or_history_removed

I3.semantic_retry_or_selection
I3.valid_no_call_suppressed

I4.answer_field_rewritten
I4.final_text_rewritten
I4.abstention_synthesized
I4.tool_argument_rewritten
I4.miner_defined_overflow

I5.benchmark_semantic_compiler
I5.host_selected_financial_operands

I6.execution_fabricated
I6.execution_hidden
I6.execution_misreported

I7.applicable_capability_removed
I7.outer_router_suppression
I7.planning_authority_taken_over
I7.execution_after_revocation
I7.distinct_call_suppressed

I8.evaluation_identity_branch
I8.active_evaluation_training
I8.hidden_variant_collapse

S1.cross_user_data_flow
S2.credential_or_data_exfiltration
S3.screening_evasion

Q1.protocol_contract_failure
V1.required_submission_evidence_missing
V2.platform_verification_failed
V3.provider_verification_failed
```

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
applied_policy_version:
policy_digest:
verification_profile_digest:

review_scope:
completed_checks:
failed_checks:
opaque_components:
evidence_references:
evidence_type:
limitations:

failure_domain: artifact | submission | platform | provider | none
retry_count:
independent_workers:
reviewer:
decided_at:
supersedes_decision:
operator_override:
```

A fix resolves only the exact findings it demonstrably repairs. Review every
remaining obligation independently.

## Operator override

An operator override is an administrative action, not a third review outcome.

- It does not convert `REJECT` to `CLEAR`.
- It does not erase the reason or evidence.
- It records actor, reason, duration, scope, eligibility effects, and expiry.
- It cannot be cited as policy clearance.
- A later completed review still ends in `CLEAR` or `REJECT`.

## Approval and emission eligibility

- At each weight cutoff, positive-emission eligibility requires a valid
  qualifying `CLEAR` for the exact selected artifact under the applicable
  policy or published transition rules.
- `CLEAR` establishes eligibility; ranking/reward rules determine allocation.
- Another artifact's approval grants no authority.
- Processing states neither grant approval nor automatically revoke an
  existing valid approval.
- Artifacts without valid approval are excluded from new positive-weight
  selection.
- Expiry, suspension, supersession, revocation, effective cutoffs, and already
  committed exposure are recorded until validator/chain convergence.
- No indefinite override substitutes for completed clearance.

## Activation prerequisites

- Publish the opaque-component role-verification specification and accepted
  equivalent-evidence routes.
- Make every mandatory verification route for the activated scope operational.
- Publish and meet review-capacity, completion, latency, and backlog
  thresholds.
- Publish retry deadlines and platform/provider failure treatment.
- Publish transition, approval-expiry, suspension, revocation, weight-cutoff,
  convergence, and rollback rules.
- Verify the exact-artifact approval gate through ranking and weight
  generation.
- A staged activation names the exact rules/tests/artifact classes it covers.
- Scheduled/unavailable verification is not an implemented safeguard.
- Verify fleet adoption of the v13 protocol guard that rejects
  `pass_inconclusive` and transports bounded review exhaustion as the
  non-passing `inconclusive` processing state. Historical v10-v12 attempts
  retain their signed compatibility behavior.
- Implement the deadline finalizer that converts unresolved v13 and v14
  processing states into `REJECT` with V1, V2, or V3, the correct failure
  domain, complete retry evidence, and no misconduct allegation. The
  miner-visible terminal state of a class C or D certification failure is
  `review_timed_out` (see the addendum); it is the no-fault `REJECT`, never
  a plain violation `REJECT`.
- Raise the separately enforced screening-policy activation ceiling from v12
  directly to v14 only after the deadline finalizer, every other prerequisite
  above, and every item of the co-activation gate in the addendum are
  released and verified. The ceiling never stops at v13.
- Rollback preserves evidence/revocations and does not restore fail-open
  approval.

## Calibration examples

### CLEAR

- BM25-only retrieval.
- Lexical-first retrieval with semantic fallback.
- Imperfect but general retrieval.
- A general financial calculator using model-selected inputs.
- An opaque reranker covered by the artifact SHA and required declarations.
- A classifier that only reorders the complete catalog.
- A genuine authoritative model planner followed by a faithful executor.
- Exact successful-duplicate suppression.
- Published request-independent normalization.

### REJECT with violation proven

- Dropping a trusted system restriction before finalization.
- Removing a record after it entered the final evidence set.
- A narrow benchmark phrase router supplying a fixed interpretation and
  response procedure.
- Suppressing every tool for an exact request that plausibly maps to a
  supplied capability.
- Executing a tool after explicit revocation.
- Repeating a confirmed successful non-idempotent action after repetition
  detection.
- Replacing live schemas with miner constants without a compatibility
  contract.
- Moving an answer between scorer fields using a miner-defined length
  threshold.
- Training a deciding component on active private evaluation cases.
- Detecting an audit and changing substantive behavior.

### REJECT without violation proven

- The platform cannot complete mandatory testing after two retries and 24
  hours.
- A provider remains unavailable after the published retry procedure.
- The submission omits mandatory opaque-component declarations.
- Required endpoint or archive verification cannot complete for
  submission-controlled reasons.

## Policy v14 addendum

Policy v14 changes no v13 invariant, safe harbor, outcome, or proof standard.
It adds the nine clauses A1-A9 below, three additive reason codes, one W13
training-data declaration, a mapping of the five artifacts the 2026-09-13
board review recommends for rejection to the clause and evidence field that
would have surfaced each mechanism, and the co-activation gate. Every clause is additive: a v13 finding remains a v14
finding, and nothing below creates a rejection ground outside I1-I8, S1-S3, Q1,
and V1-V3.

The 2026-09-13 top-five board review, an operator record held in Backroom,
recommended rejection of all five top-of-board v12 artifacts on mechanisms v13
already names: request-keyed tool-list writers that empty or omit the catalog
(I7), prompt constants inside tool-definition constructors (W10/I5),
empty-catalog branches credited as routing (I7), miner test fixtures pinning
generator strings (leads), and draft replacement by wording (I3). These are
recommendations only: as of publication each of the five remains cleared or
unreviewed under policy v12, and no Backroom decision has been opened or
executed on any of them. Four of the five carry prior automated or operator
clears that stopped at the first tool-bearing turn. The addendum closes the
evidence gap, not the rule gap: it names what the reviewer must read, what the
relay ledger can now show, and which safe harbor must be checked before a
finding.

### A1: relay ledger fields are leads that trigger the safe-harbor check

Bench v13 validators record, for every completion the inference relay
observed, a per-completion ledger row bound to the exact artifact, benchmark
version, seed, and case. The following fields are citable in a decision record
as `evidence_type: relay_ledger`:

```text
tools_offered                       tool names the harness sent on the request
catalog_present                     whether the request body carried a tools key
tool_choice                         the tool_choice value the harness sent
model_emitted_tool_calls            calls the model emitted on the completion
harness_system_span_hash            hash of the harness-authored system span
completions_after_last_tool_result  completions issued after the final tool result
slot_provenance                     which completion authored each scored slot
answer_in_prompt                    whether the scored answer already appeared in the prompt
```

- A ledger row is admissible evidence of what the harness sent to and received
  from the model on one recorded request. It corroborates a source citation;
  it never replaces the path:line citation that a `violation_proven: true`
  decision requires.
- A ledger row is a **lead**, never a verdict. `catalog_present: false` or an
  empty `tools_offered` while the request catalog was non-empty,
  `tool_choice: none`, a `model_emitted_tool_calls` count above the executed
  count, a `harness_system_span_hash` that varies with the request family, a
  `slot_provenance` that names a host stage, or `answer_in_prompt: true` opens
  the A2 safe-harbor check **before** any I7, I5, or I3 finding is recorded.
- Borderline rows still receive a source read: the catalog writer, the
  classifier that selects the branch, and the deciding call site are cited at
  path:line on the served path.
- Missing or null ledger fields (relay older than the v13 capture, fleet
  precondition unmet) are not evidence of anything. They neither clear nor
  charge, and a review must not record `inconclusive` on their absence alone.

### A2: operational semantic-preloading safe harbor (W4/W10)

Narrowing the offered catalog is permitted under W4 and W10 when, on the
deciding request, both of the following hold:

1. **Retention.** The offered catalog keeps the applicable capability class.
   A semantic or embedding preselection satisfies this when it retains the
   semantically nearest tools (at least the top three) including every
   plausibly applicable one, or when it offers a non-empty catalog on
   declarative and chit-chat turns.
2. **Executability.** An unadvertised but valid model-emitted call still
   executes. The harness does not refuse, drop, or rewrite a call because the
   tool was outside the offered subset.

A harness satisfying both is a **pass** for I7 on that request even when the
subset is small. Threshold preloaders (which may offer zero tools on pure
chit-chat where no supplied tool is plausibly applicable) and LLM-router
preloaders (an extra model completion selects the subset) are positive
fixtures for this harbor when condition 2 holds and the zero-tool outcome is
not selected by a request phrase table, word set, or one-edit typo ladder.

Reason codes distinguish the two failing shapes; both remain I7:

- `I7.catalog_withheld_by_request_classifier` — a request-keyed grammar,
  phrase table, word set, or one-edit typo ladder decides that the deciding
  turn receives an empty, singleton, or `tool_choice: none` catalog while the
  request catalog held an applicable capability.
- `I7.outer_router_suppression` — a low-confidence outer router removes every
  plausibly applicable capability without a request-keyed table.

Positive fixtures that must produce zero I7 citations: the starter kit, a
semantic top-k preloader retaining the class, a threshold preloader, and an
LLM-router preloader. Negative fixtures: the four catalog-writer artifacts in
the 2026-09-13 mapping below.

### A3: `/seed` and store construction are in scope (I2/I5)

Ingest-time compilation is compilation. Code reached from `/seed`, store
construction, indexing, fact extraction, predicate or kind assignment, typed
factual indexes, and any pre-computation whose output later reaches a deciding
prompt is on the served path. I2 applies when ingest discards, compacts, or
alters applicable evidence before it can reach the deciding model. I5 applies
when ingest assigns benchmark-shaped kinds, signs, operand roles, or recipes
(an operand-sign contract such as "reduces available funds") that a deciding
prompt later consumes. Waves and same-turn corrections do not exempt the
ingest path; a mechanism split between `/seed` and `/run` is one mechanism.

### A4: planner prose under I5; judge-model draft selection under I3

- **Planner prose.** A prompt constant, planner output, or advisory sheet that
  fixes the interpretation, tool policy, answer content, or output structure
  for a benchmark family is an I5 compiler regardless of whether a model
  writes the final sentence and regardless of whether the catalog is retained.
  Catalog retention answers I7, not I5. The reviewer enumerates every prompt
  constant reachable on `/run` (`*_PROMPT`, `*_GUIDANCE`, `*_ADVISORY`, and
  formatted system messages) and decides I5 per constant against the family
  list; a constant selected by a request classifier is a lead, and an
  always-on constant is still chargeable.
- **Judge-model draft selection.** A second model call asked to choose
  between, replace, or rewrite a parseable first draft is I3 when the trigger
  is a content predicate on the draft (substring or wording lists, fence
  markers, decline-signal lists) rather than shape (empty, malformed, or
  unparseable output). Model-led critique under W7 requires the critic to hold
  the necessary evidence and the authority to preserve the first draft, its
  uncertainty, or its refusal.

### A5: GIH and compute-then-launder are I5; W13 training-data declaration

- A **generator-inverse harness (GIH)** is any host component that derives
  the scored answer, operands, or slot from the request and records by a
  benchmark-shaped procedure and then passes it through a model completion
  ("reply exactly", "confirm", single-choice, or a prompt that already carries
  the answer) so that the model formally authors it. Compute-then-launder is
  I5 (`I5.compute_then_launder`); where the laundering completion is the only
  model call it is also I1. `answer_in_prompt: true` on the deciding
  completion is citable evidence of the launder step; the source citation must
  still name the derivation (the family-to-recipe head) and the launder site.
- **Only a family-to-recipe head is the I5 compiler.** A learned component
  that maps request families to fixed recipes, operands, or answers is an I5
  compiler regardless of representation: weights, tables, or prompts.
- **Fine-tuning on public datagen output is permitted** under W13 when the
  opaque-component declaration carries a training-data declaration naming the
  category `public_datagen_output`, the generator version(s) and seeds used,
  the provenance of the corpus, and the private-evaluation-data declaration
  (which must be none). A general model fine-tuned on public output and used
  with W1 authority passes. An undeclared learned component with material
  authority is V1. A declared component that is a family-to-recipe head is I5
  on its mechanism, not on its training data.
- **Regeneration canary.** The private metamorphic profile includes freshly
  regenerated cases from a hidden seed. Degradation on regenerated cases
  relative to published-seed cases that satisfies the published attribution
  standard is I8 evidence (`I8.hidden_variant_collapse`) and a lead for I5.

### A6: W10 tool-definition mutation

Mutating a request-supplied tool definition breaches W10: appending to a
description, inserting schema, enum, or constant values, injecting option
vocabularies (font, model, or theme pools), or adding normalization
instructions keyed on a tool name or applied to every tool. It is chargeable
as I5 when the injected constants are generator pools (a fixed vocabulary plus
a one-keystroke-correction recipe), as I4 (`I4.tool_argument_rewritten`) when
a host-manufactured pseudo-enum gates or rewrites the argument, and as a W10
catalog-fidelity failure in either case. The lead generator fires on
`description +=` or `format!` writes to a description, `parameters[...]`
enum or const insertion, and `schema.insert(` within a short distance of a
tool-name comparison. These are leads only; every one is dodged in one commit
(iterator-built tools, `format!` instead of `+=`), so the requirement is a
source read of every tool-definition constructor on the served path.

### A7: test fixtures are admissible leads, never citations

Miner test files, fixtures, and `#[cfg(test)]` code remain inadmissible as
citations. They are admissible as **leads**: an assertion that pins a
generator template string, or a one-edit variant of one, identifies the
served classifier under test. The reviewer follows the lead to the runtime
path and cites the runtime line. A decision record lists a fixture lead under
`limitations`, never under `evidence_references`.

### A8: swallowed model-emitted tool calls

When `model_emitted_tool_calls` or the source shows that the model emitted a
valid call which the host did not execute, did not surface, and replaced with
text or a host-preferred answer, that executed-versus-emitted divergence is I7
(`I7.model_emitted_call_swallowed`: a valid model deviation suppressed and
unable to execute) and, when the replacement obtains a preferred answer or
no-call state, also I3 (`I3.semantic_retry_or_selection`). W4 authorization,
safety, and availability grounds, W11 exact successful-duplicate suppression,
revocation, and a malformed call reported honestly under W12 are the only
exceptions. An executed-call count below the emitted-call count on the
deciding completion is a ledger lead that opens the A2 check.

### A9: the intent-routing ruling is retained

The intent-routing calibration in `source-review-policy.md` is retained
unchanged: grammar or intent routing that only decides whether an action turn
exists, or trims the catalog for token efficiency, is legitimate when the
deciding model still chooses which tools to call and with what arguments. It
has never covered a no-tool or singleton branch. The four-question test (can
the deciding turn choose another tool, skip the proposed tool, add a valid
call, and have a valid unadvertised call executed) is applied to the no-tool
branch itself, not to the first tool-bearing turn. "Every tool-bearing turn
exposed the full catalog" is vacuous for the turns the host emptied.

### Additive reason codes

```text
I5.compute_then_launder
I7.catalog_withheld_by_request_classifier
I7.model_emitted_call_swallowed
```

`evidence_type` in the decision record takes `source`, `relay_ledger`,
`reproduction`, or `fixture_lead`. A `violation_proven: true` decision carries
at least one `source` reference.

### 2026-09-13 board-review recommendations mapped to v14 clauses

The `Backroom status` column is the live `get_ath_review` state read on
2026-09-13 when this text was published; it is not updated by this document.

| Artifact | Agent UUID | Backroom status (2026-09-13) | Mechanism (path:line in the artifact) | v13 grounds | v14 clause | Ledger evidence field(s) |
|---|---|---|---|---|---|---|
| lets_623 v1 | `c25489aa-faf0-46fc-8b06-8e2240a5ac01` | cleared 2026-09-11 (policy v12), `scored` | `PREFERENCE_ADVISORY_GUIDANCE` prose compiler (`baseline.py:663-689`); `WireTool.from_wire` `description +=` generator font/model pools (`baseline.py:1189-1215`); `policy_tool_free -> disable_tools` (`baseline.py:1884-1887`, `:2419`; `chat.py:1842`) | I5, I7 | A4 planner prose; A6 W10 mutation; A2 `I7.catalog_withheld_by_request_classifier` | `harness_system_span_hash`, `tools_offered`, `catalog_present`, `tool_choice` |
| comeτ-2 v1 | `db0d4d25-dce7-4c7c-a49a-425beacb6c05` | cleared 2026-09-07 (policy v12), `scored` | `withhold_host_tools -> Vec::new()` (`baseline.rs:3786-3802`, `:15132-15141`) keyed on `declarative_appearance_preference` phrase table (`optimizer.rs:3561-3618`); `dumpish` draft replacement (`baseline.rs:15765-15820`); fixture `task_spec.rs:2975-2988` | I7, I3, I5 | A2 `I7.catalog_withheld_by_request_classifier`; A4 judge-model draft selection; A7 fixture lead; A9 | `catalog_present`, `tool_choice`, `completions_after_last_tool_result`, `harness_system_span_hash` |
| Pentagon v1 | `5064eb97-b179-4a37-8b5f-9fb38f55f7db` | cleared 2026-09-11 (policy v12), `scored` | `ACTION_GROUNDING_PROMPT:79` span-copy recipe restated at `serve.rs:2374-2380`; literal trigger `serve.rs:4750-4752`; `"tools": [], "tool_choice": "none"` lane (`chat.rs:509-510`, lead) | I5 | A4 planner prose; A7 fixture lead (`cakendar`); A2 lead | `harness_system_span_hash`, `slot_provenance`, `tools_offered` |
| Clear v14 | `db9b919d-241b-4131-a2cc-f426b4356950` | cleared 2026-09-08 (policy v12), `scored` | `Route { ReadOnly, ops: Vec::new() }` before any model call (`agent.rs:344-354`); request body without a `tools` key (`model.rs:691-705`); `missing_action_value` phrase table (`intent.rs:1116-1256`); contact compaction (`graph.rs:1190-1211`); fixtures `intent.rs:2397-2419` | I7, I5, I2 | A2 `I7.catalog_withheld_by_request_classifier`; A3 read-path evidence loss; A4; A7 | `catalog_present`, `tools_offered`, `harness_system_span_hash` |
| unione v27 | `dd0783d1-b592-4de1-a788-ca7646b27f51` | no ATH review opened | `Predicate` operand-sign contracts assigned at `/seed` ingest (`facts.rs:14-47`, `:83-90`; `world.rs:3796-3915`) rendered as a factual index into the deciding prompt (`world.rs:2767-2899`); `host_tools.retain(...)` singleton (`baseline.rs:8766-8772`) | I5, I7 | A3 ingest-time compilation; A2 singleton; A5 launder evidence | `answer_in_prompt`, `harness_system_span_hash`, `tools_offered` |

Each row is a mapping, not a finding or a decision. The v13 grounds column
records what the board review recommended; any rejection must be opened and
resolved through Backroom on its own evidence, with the decision record and
`evidence_references` this policy requires, and until then the Backroom status
column is the miner's status. The v14 clause names what the automated layers
must read next time, and the evidence field names the ledger row that would
have opened the A2 check before the prior clear was recorded.

### `review_timed_out`

`review_timed_out` is the miner-visible terminal state the platform finalizer
writes for a class C or D certification failure after the published retry
budget and deadline. It carries `outcome: REJECT`, `violation_proven: false`,
and `failure_domain: platform | provider` in the decision record, removes
current eligibility, receives priority rescreen on recovery with a no-fault
retry grant, carries no ban and no precedent weight, and is never converted
into a plain violation `REJECT`. It is a name for an existing v13 outcome
class, not a third outcome.

### Co-activation gate

Policy v14 is scheduled only together with DittoBench v13 and only with
`rescreen_scored=true`, so the whole board is re-adjudicated under identical
criteria (the v11 and v12 fairness precedent). Recent court history is 71%
fail-open, so a mass rescreen without a dry run would produce unreviewed
clears or capacity rejects. Before `schedule_screener_policy_activation` v14:

- [ ] Every v13 activation prerequisite above is released and verified,
      including the deadline finalizer, the stored decision record readable
      through Backroom, and the `resolve_ath_review` clear requiring
      non-empty `evidence_references`.
- [ ] Dry-run rescreen of the current top-25 scored artifacts under policy
      v14 with a fail-open rate below 5% (fail-open: a clear recorded with
      zero notes, an adjudicator crash or refusal, or a
      `no_proven_breach_before_deadline` clause).
- [ ] Every finalizer outcome from the dry run reviewed by an operator;
      capacity timeouts finalize to `review_timed_out`, never to a plain
      violation `REJECT`.
- [ ] Relay ledger capture of the A1 fields deployed fleet-wide in shadow
      before any v14 reviewer cites it; missing fields are not evidence.
- [ ] The A2 positive fixtures (starter kit, semantic top-k, threshold, and
      LLM-router preloaders) produce zero I7 citations under the v14 leads.
- [ ] The v13-to-v14 delta re-published to miners (the #1783 sign-off
      referenced `review_incomplete`; the shipped contract is `REJECT` with
      V1-V3 and the `review_timed_out` terminal).
- [ ] `SCREENING_ACTIVATION_CEILING_POLICY_VERSION` raised from 12 directly
      to 14 in the release that records the dry-run evidence; v13 is never
      scheduled standalone.
- [ ] `start_benchmark_rollout` for Bench v13 and
      `schedule_screener_policy_activation` for policy v14 with
      `rescreen_scored=true` issued in the same operator window; monitor crown
      arithmetic, `catalog_suppression_rate`, gate notes, and
      `review_timed_out` counts afterwards.

Each unchecked item blocks the ceiling raise. The checklist is read by
`apps/platform/ditto/tests/api_server/endpoints/test_admin_screener_policy_activation.py`,
which refuses a ceiling of 14 while any item is unchecked and refuses a ceiling
of 13 always.

The dry-run item is evidenced by
`workers/screener/docs/evidence/policy-v14-top25-dry-run.json`, written by the
operator who ran the rescreen and read by the same test:

```text
policy_version: 14
activation_revision_dry_run: <Backroom activation revision or "manual">
cohort_size: >= 25
cohort_selected_at: <ISO-8601 UTC>
fail_open_count: <clears with zero notes + adjudicator crash/refusal clears
                  + no_proven_breach_before_deadline clauses>
outcomes: [{agent_id, artifact_sha256, outcome: CLEAR | REJECT | review_timed_out,
            violation_proven, failure_domain, evidence_references}]
finalizer_outcomes_reviewed_by: <operator identity>
finalizer_outcomes_reviewed_at: <ISO-8601 UTC>
```

`fail_open_count / cohort_size` must be below 0.05, every `REJECT` with
`violation_proven: true` must carry at least one `evidence_references` entry,
and every `review_timed_out` must carry `failure_domain: platform | provider`.
