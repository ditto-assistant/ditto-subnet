# SN118 source-review policy v13

Status: published with partial screener support; activation pending. This policy
takes effect only through the versioned activation control after every
prerequisite at the end of this document is satisfied.

## Source-review admission amendment

The built-in admission profile uses exact-artifact source review and the
mechanical build, image, and health gates. A private behavioral challenge is
not a universal admission requirement. A completed, certified low-risk source
review can clear without one; a supported source violation remains held for
evidence-bound rejection review. Missing source evidence is never a clear.
Until Platform verifies source-only clear receipts, a v13 court clear is
transported and held as `QUARANTINE` with
`source-review-awaiting-v13-verification`.

Operators may still request a bounded private behavioral audit for a specific
unresolved runtime question. Such an audit has its own explicit manifest and
does not silently become a prerequisite for every submission. The activation
record must include the revised built-in manifest digest and release revision;
historical decisions retain their original policy digest.

A completed eligibility decision is `CLEAR` or `REJECT`. A reviewer, platform
or provider failure leaves verification incomplete and the attempt parked for
operator review or manual retry; it does not establish rejection evidence.
An artifact-controlled certification rejection records `violation_proven:
false` and must never be described as cheating, fraud, or deliberate evasion.

## Final outcomes

Policy v13 has exactly two final outcomes:

```text
CLEAR
REJECT
```

Queued, building, screening, retrying, and adjudicating are processing states.
A completed eligibility decision is `CLEAR` or `REJECT`. When screening cannot
complete, the attempt ends once and the submission stays `screening_failed`
for explicit operator review or manual retry. That state confers no clearance.
Repeated inconclusive reviews and platform/provider failures are evidence of
verification problems, never grounds for automatic rejection or misconduct.
There are no automatic cost-bearing retries; see `docs/no-automatic-retries.md`.

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

- Policy v13 takes effect only through a published activation revision.
- The activation record contains the exact policy digest and activation time.
- Existing v12 results remain historical v12 results.
- Applying v13 to an existing artifact requires an explicit v13 rescreen.
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
- The model receives the request and its selected evidence/context under W3;
  completeness of raw evidence is not itself a model-authority requirement.
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

A summary, digest or checkpoint may carry an instruction when it preserves
its applicable meaning, scope and restrictions. Omitting raw instruction bytes
alone is not a violation; removing an applicable trusted restriction with a
material consequence remains prohibited under I2.

### W3: retrieval and final evidence

Allowed retrieval includes BM25, lexical search, vector search, graph
retrieval, cross-encoder reranking, learned reranking, lexical-first semantic
fallback, semantic-first lexical fallback, bounded top-k selection, and
multistage candidate selection before substantive reasoning.

Policy v13 does not require perfect recall. Retrieval uses the authenticated
principal's authorized data.

Records, tool arguments, receipts and history may be selected, excerpted,
summarized, compacted, checkpointed or omitted. The same permission applies
before and after a deciding or critic model has received or used them, and to
retries, recovery and fresh fallback transcripts. There is no requirement to
retain every raw record, receipt or prior turn at every later stage.

Keeping corrections, qualifications, contradictions, chronology and execution
state is useful engineering. Losing them can reduce answer quality or cause a
reliability failure. Evidence loss alone is a benchmark-quality issue, not an
integrity or certification rejection ground. Neither a pointer to unavailable
evidence, an incomplete summary nor a fresh fallback is sufficient proof of a
violation. Context limits prove neither compliance nor a violation.

Applicable trusted instructions, authorization and safety constraints remain
protected under W2/I2. Independently prohibited mechanisms remain enforceable,
including host semantic suppression of a valid designated answer (I3), host
answer rewriting (I4), a proven benchmark-specific recipe (I5), execution
misrepresentation (I6), or unauthorized capability changes (I7). Each needs
its own complete proof and material consequence; loss of evidence is not that
independent ground.

#### Clarification: summaries, compaction and recovery (2026-10-25)

Evidence loss from retrieval, summaries, compaction, checkpointing, retries or
recovery alone is benchmark quality or reliability, not rejection. This
includes losing a receipt after a successful deciding call consumed it,
switching to a fresh model-authored fallback, and a possible different answer
because the later model has less context. A hypothetical sequence showing
that loss, or even an observed wrong answer from it, does not establish an
independent prohibited mechanism.

A rejection must identify that separate mechanism under its published rule
and prove its material response, execution or security consequence. Do not
rename incidental evidence loss as I2, I3, I5, I6, Q1 or another condition.
This does not permit ignoring applicable trusted instructions/authorization,
overriding a valid designated model answer to obtain a preferred value,
fabricating execution, or a separately proven security breach.

The 51 fallback-only rulings identified in the dated operator withdrawal
record are withdrawn as precedent. Their exact UUID/SHA scope is recorded in
the Backroom review precedent store. Restoration is an audited court action
with fresh guards and readback; this document neither applies it nor erases
the historical rejects. A different, independently supported ground remains
subject to its own exact-artifact review.

#### Clarification: over-budget retrieval and record selection (2026-10-23)

Choosing which retrieved records reach the deciding input is retrieval
quality, not, by itself, a rejection ground under W3, I2 or any other
condition. This covers a size budget that drops an oversized record, keeps an
excerpt or its opening lines, or leaves an ID or pointer in its place. It
also covers deduplication or collapsing of similar records (even one that
drops a later distinct record), identifier pinning that shows only matching
records, event projections or summaries in place of records, field
projections that strip a field such as the saved timestamp from every record,
and top-k selection. These are design choices, often poor ones. Making them costly is
the benchmark's job (bench v14 will reward keeping such records), not the
screener's. The model, not the host, still authors the graded value or tool
call. A content-dependent selector is not automatically a breach either;
prove an independent prohibited mechanism and material consequence under the
October 25 clarification rather than inferring them from an omitted record.

Independent grounds that still reject:

- the host writes, rewrites or overrides the graded value or a dissenting model
  answer, or presents host-derived state as authoritative (I3, I4);
- execution is fabricated or misreported (I6);
- tool capabilities are removed or forced (I7); or
- a separate benchmark recipe (I5).

The 2026-10 rejections that rested only on record selection are withdrawn as
precedent. A rejection that also proved an independent ground keeps that
ground.

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
- the model may inspect the selected evidence/context under W3; no mandatory
  raw-record retention is inferred from this calculator safe harbor;
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
  drafts, which may be summarized or checkpointed under W3. Losing a raw
  receipt or draft alone does not turn a declared critic into a violation.
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
permitted length-only model reserialization uses selected/summarized context
under W3 and no expected semantic answer; its first compliant result ships.

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

The declaration may combine submission evidence with a platform-published,
content-addressed provenance record that predates the review. An exact path and
digest match to a named official starter-kit component satisfies only the
fields covered by that record and its referenced public documentation. The
submission does not need to duplicate those fields inside its archive.

- The complete artifact SHA covers every component.
- Review caches include the artifact SHA and policy digest.
- Changing one byte invalidates cached artifact review.
- Moving a matched component, changing its selecting configuration, loader,
  runtime role, candidate boundaries, or downstream authority requires fresh
  evidence for the changed property even when the component bytes are stock.
- Presence or loading of an exact official-component match is not by itself
  missing submission evidence and cannot by itself support `V1`.
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

- When a private challenge is requested, artifact commitment precedes its
  generation and randomness.
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

### I2: applicable trusted instruction or authorization loss

Reject when a deciding, retry, critic or fallback stage loses the applicable
meaning of a trusted instruction, authorization boundary or safety constraint,
and that loss has a proven material consequence. Meaning-preserving summaries,
projection and checkpoints are allowed. Raw evidence/history loss alone is
not I2, including after a model has successfully consumed a receipt (W3).

A rule that suppresses a valid designated model result because it disagrees
with an expected answer must independently satisfy I3. A rule that forges or
misreports execution must independently satisfy I6. Neither may be inferred
from a less complete fallback transcript alone.

Required proof:

1. exact UUID and SHA;
2. exact trusted instruction, authorization or safety constraint;
3. why it applies to the actual served stage;
4. where its meaning entered the request;
5. where that meaning or restriction was removed or changed;
6. the actual deciding input and effective execution guard lacking it; and
7. the resulting material response, execution or security consequence.

The proof also names the reachable path. Ordinary relevance ranking, an
isolated miss, receipt omission or a hypothetical retrieval/recovery miss is
insufficient. Controlled exact-artifact reproduction may prove a reachable
mechanism and consequence; it does not turn evidence loss alone into I2.

#### Clarification: which instructions I2 protects (2026-10-19)

I2 protects instructions that the served request actually carries. It is
not a requirement that every model call repeat the system prompt.

- **Name the instruction.** An I2 trusted-instruction finding quotes the exact
  instruction text from the request being served and shows why it applies to
  the stage that lost it. Naming a category of instruction that a request
  could carry ("scope, disclosure, temporal, authorization or output") is
  hypothetical, the same as a hypothetical retrieval miss, and is
  insufficient.
- **The deciding input decides the question.** W2 requires the instruction at
  every deciding answer-producing call. A model stage before that call (a
  draft, reading, ordering, extraction or lookup pass) whose output reaches a
  deciding call that still carries the instruction breaches I2 only when the
  proof shows a material alternative that the deciding call can no longer
  evaluate. Show that the named instruction would have changed that stage's
  output, or reproduce the change on the exact artifact. Labelling the stage
  "advisory" is neither a defense nor a breach.
- **What the request carries.** In Bench v13, every scored `/run` carries the
  validator-authored system prompt "You are Ditto, a helpful assistant with
  access to tools. Call a tool only when it is the right action for the
  user's request." A tool-less model stage cannot breach that instruction's
  tool rule. Confirmation lanes can carry other system prompts, such as the
  LongMemEval current date. Cite the lane and text that apply.
- **`/seed` is out of scope for this clause.** The seed request carries no
  system prompt, so seed-time processing cannot lose a trusted instruction.
  Model-written notes, extraction, indexing and summaries built during
  `/seed` are permitted on this axis. They remain subject to W3, I5 and the
  other independently applicable conditions; missing originals alone do not
  establish an instruction-loss breach.

Passing the request system prompt to every `/run`-time model stage remains
good practice. Omitting it is a lead to examine, not a finding.

The 2026-10 board holdings that charged I2 for a preliminary model stage
without naming an applicable instruction from the served request do not meet
this standard. They are withdrawn as precedent.

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

#### Clarification: case-file currency rules (2026-10-23)

Bench v13 case files shuffle their records and then stamp filing timestamps
in shuffled order (`research/dittobench-datagen/universe/personal.go:561-580`,
`universe/v13_contract.go:1056-1072`, `universe/v13_calendar.go:26-33`). A
plain restatement can therefore be dated after the change note that replaced
it, which ordinary conversation does not do. This is a benchmark realism
defect tracked for Bench v14 in ditto-subnet#3088. Coping with it is not an
I5 violation.

A **case-file currency rule** is not, by itself, a rejection ground under I5,
W3 or any other condition. Such a rule may:

- treat a record that states its own change, correction, handover or
  replacement as current over a plain restatement of the same topic, whatever
  their filing dates;
- tell the deciding model that filing dates may not show the order in which
  things were said;
- recognize change statements by change vocabulary, including wording that
  matches the public generator's change rows;
- scope the comparison to the records that share an identifier the request
  names, such as a case-file id, and serve only those records when the request
  itself limits the answer to them;
- leave an aside that a record marks as separate or unrelated out of that
  comparison;
- show the deciding model a host-computed note or added turn naming the
  "current" statement, what it "supersedes", or the resulting current list;
- omit the filing times of the records in that scoped view, while their text
  stays in the deciding input.

These elements are one behavior. Combining several of them is not a second
factor. A reject of a submission that carries such a rule needs an
independent ground that stands with the rule removed, proven under its own
condition. Examples:

- the host writes or rewrites the graded value from the currency result
  instead of the model authoring it (I4), or retries or overrides a model
  answer that disagrees with the candidate (I3);
- the currency result removes or forces tool capabilities (I7); or
- a separate benchmark recipe, such as an answer-family or operand compiler
  (I5).

Outside the benchmark, a real save time is genuine chronology, so the rule can
mislead a deployed agent. That is a product-quality lead, not an integrity
finding.

The 2026-10 board holdings that rejected a submission only for this shape
(the "SN-215 shape" or "case-file currency recipe") are withdrawn as
precedent. A holding that also proved an independent ground keeps that ground.

#### Clarification: routed instruction guidance (2026-10-23)

A request classifier or router that chooses which general instructions,
examples, checklists or tool notes the deciding model sees is not, by itself,
a rejection ground under I5 or any other condition. It may route by request
type, tool family, answer form or the request's phrasing. Tailoring
instructions to the kind of request is ordinary assistant engineering, and
these mechanisms are useful product behavior. This holds when:

- the deciding model still receives the request, the record text it needs and
  the tool catalog, and authors the graded value or tool call itself;
- the guidance is advisory text: the host does not act on the classifier
  result by writing, rewriting, retrying or overriding the answer (I3, I4),
  removing or forcing tools (I7), or stripping evidence a model already
  received (W3); and
- the guidance encodes no benchmark-private knowledge, such as hidden answers,
  private case or seed values, or grader internals that the repository does
  not publish.

Guidance may state an outcome rule that the public generator or tool
semantics publish. Examples: "if the stored setting already matches the
request, say so instead of calling the setter; if the records leave the
choice open, ask"; "search the calendar before adding an event that may
already exist". These describe sensible assistant behavior. Telling the model
about them is not I5, even when they match how the benchmark grades. The
classifier may be hand-written rules, regular expressions or a small compiled
model declared under W13. Several routed notes together are one behavior, and
stacking them is not a second factor.

The I5 phrase "a family classifier activates a fixed benchmark answer or
interpretation recipe" therefore means host-enforced output or
benchmark-private content, not advisory guidance the model may follow. A
reviewer that finds only routed guidance resolves those I5 leads as refuted
under this clarification rather than leaving them unresolved.

A reject of a submission that routes guidance needs an independent ground,
proven under its own condition. Examples:

- host code parses benchmark event text into operands, steps, ordering or
  answers and serves that result in place of the model's own reading (I5);
- the host writes, edits or overrides the graded value or a dissenting model
  answer (I3, I4);
- the route removes, hides or forces tool capabilities (I7);
- the guidance carries benchmark-private content.

Guidance that misfires outside the benchmark is a product-quality lead, not an
integrity finding.

The 2026-10 holdings that rejected a submission only for routed decision
notes, per-family instruction modules or settings-request predicates (the
SN-214 settings predicates included) are withdrawn as precedent. A holding
that also proved an independent ground keeps that ground.

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

Apply V1 only after combining the artifact's evidence with applicable accepted
platform evidence. Do not assign V1 because an archive omits a README,
metadata sidecar, or self-report whose required facts are already bound by an
exact accepted component path and digest.

This is a certification failure, not proof of misconduct.

### Platform and provider verification not completed

Historical V2/V3 rejections and the reason code
`verification-incomplete-unreviewable` remain readable for audit. Do not issue
new rejections from repeated incomplete reviews or infrastructure/provider
failures. Counting reviews does not supply missing evidence or prove that the
artifact violates a requirement.

- Record the exact missing verification, failure domain and retained evidence.
- End the dispatched attempt and park it for operator review or manual retry.
- Do not infer an unpublished submission requirement from reviewer difficulty.
- Do not pass unread source or automatically schedule another billed review.
- A rejection still requires independently supported artifact-controlled
  contract failure (V1) or a proven integrity/security breach.

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

A null or omitted field in a score, leaderboard, or other compact projection
proves only that the projection did not supply the field. It does not prove
that an artifact-bound screening or challenge record is absent. Check the
authoritative evidence store before assigning V1; V2/V3 are historical readback
only.

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
14. fallback instruction/authorization safety and truthful execution reporting
    (not mandatory raw evidence/history retention);
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

Platform verification did not complete. The failed attempt stays parked for
operator review or explicit manual retry. It is neither clearance nor rejection
evidence and cannot be cited against siblings, owners or hotkeys.

#### D. Provider verification failure

```text
violation_proven: false
failure_domain: provider
```

Provider verification did not complete. Record that failure, end the attempt,
and retain evidence for the same manual review/retry treatment.

### Non-decisive results

A review that reaches no decision is not a clearance. None of the following
may produce `CLEAR`, and none of them may submit a passing verdict:

- `source-review-inconclusive` — bounded source review exhausted without a
  decisive finding;
- `source-review-invalid-risk` — the reviewer returned an unusable risk level;
- `source-review-inconsistent-verdict` — the reviewer contradicted itself;
- `source-review-adjudication-refused` — adjudication ended without a
  finalized, verified finding (Platform stores it as the reason code
  `adjudicated-source-review-escalate`);
- `behavioral-oracle-inconclusive` and `challenge-inconclusive` — an explicitly
  required targeted behavioral audit produced no usable observation; and
- `source-review-unavailable` — review infrastructure was unreachable.

Each ends the dispatched attempt and remains parked without admission or
rejection until an operator resolves it or issues a manual retry. A later
artifact-controlled contract failure or proven breach must have its own
supporting evidence; incompleteness alone cannot become one by repetition.

An adjudicator that fails without a finding records only that adjudication did
not complete. It is neither clearance evidence nor rejection evidence, and it
may never be reported as cheating.

A held court reject or refusal (worker evidence `source-review-adjudicated`,
with `source-review-adjudication-refused` for a refusal) retains the built
image as supplemental evidence bound to the screening attempt, never to the
agent, so artifact-bound verification can proceed without a rebuild.
`adjudicated-source-review-*` is Platform's stored reason code, not worker
evidence.

A targeted behavioral-oracle pass does not substitute for an undecided source
or build check.

All `REJECT` outcomes remove current rank and emission eligibility while
preserving scores, artifacts, evidence, and history. The decision records
exact reason codes and distinguishes proven violations from verification
failures. No owner, hotkey, sibling, or lineage penalty follows automatically.
Appeals produce a new recorded `CLEAR` or `REJECT` without erasing history.

## Manual retry and deadline procedure

A failure ends the dispatched attempt once. A retry requires an explicit
Backroom authorization bound to the exact artifact and latest terminal attempt;
classification as platform/provider failure is not retry authority.

For each manually authorized retry:

- distinguish artifact, platform, and provider failures;
- preserve bounded diagnostics and every attempt;
- verify the artifact SHA remains unchanged; and
- avoid duplicate paid work when a valid conclusive artifact-bound result exists.

The following is a recommendation for a future explicitly activated review
window, not an active failure-to-rejection rule:

```text
maximum verification window: 24 hours
```

For a future explicitly scheduled v13 deadline activation, the review-window
start event is the **first v13 screening claim for the submission UUID**. It
includes a build-only or deferred-admission claim; a later deep-review claim,
rescreen, or infrastructure retry does not restart the clock. The activation
must publish its configured duration and separately bind this normative policy
document digest and the worker's module-manifest digest before that first
claim. The 24-hour value above remains a recommendation, not an active cutoff.
An existing submission with any earlier v13 claim receives no retroactive
window from a later activation. Its transition requires a separate published
rule and exact-artifact review. A deadline alone never supplies evidence for
`CLEAR` or a verification-failure `REJECT`.

At the end of a review window, unfinished attempts remain parked for an
operator. Elapsed time and repeated inconclusive reviews supply neither
clearance nor rejection authority.

## Required reason codes

```text
I1.model_authority_bypass

I2.trusted_instruction_removed

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

Historical `I2.final_evidence_removed` and `I2.receipt_or_history_removed`
records remain readable for audit. They are not grounds for a new rejection
based only on retrieval, summarization, compaction or fallback evidence loss.

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
- Expire unresolved attempts into a parked failure with its actual failure
  domain and retained evidence. Do not turn a reviewer/provider failure or a
  count of inconclusive reviews into rejection.
- Raise the separately enforced screening-policy activation ceiling from v12
  only after the attempt finalizer and every other prerequisite above are
  released and verified. Record: the ceiling moved to v13 on 2026-09-14, after
  #1801 shipped the strict two-outcome contract and both production screeners
  reported builtin policy 13 on release 0.264.0. Raising the ceiling makes v13
  schedulable; it does not activate it, and it does not move the v10 floor.
- Rollback preserves evidence/revocations and does not restore fail-open
  approval.

## Calibration examples

### CLEAR

- BM25-only retrieval.
- Lexical-first retrieval with semantic fallback.
- Imperfect but general retrieval.
- Summaries, checkpoints, compaction, retries and fresh model fallbacks that
  omit prior raw records/receipts, without a separate prohibited mechanism.
- A general financial calculator using model-selected inputs.
- An opaque reranker covered by the artifact SHA and required declarations.
- A classifier that only reorders the complete catalog.
- A genuine authoritative model planner followed by a faithful executor.
- Exact successful-duplicate suppression.
- Published request-independent normalization.

### REJECT with violation proven

- Dropping a trusted system restriction before finalization.
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

- The submission omits mandatory opaque-component declarations.
- Required endpoint or archive verification cannot complete for
  submission-controlled reasons.
