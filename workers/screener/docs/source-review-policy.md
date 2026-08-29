# Source-review decision policy

The source reviewer identifies submissions that replace a general agent with
benchmark-, scorer-, or audit-specific behavior. Its findings select operator
quarantine; they never create an automatic terminal rejection.

Screening policy v10 made the seven strict integrity invariants below part of
the signed source-review contract. It changes neither benchmark activation nor
operator decision authority: model review may select quarantine, but only an
operator may reject a submission. Historical v9 findings retain their original
wire identity and are not silently reinterpreted; v10 applies to new or
explicitly rescreened attempts.

## Policy v11 (in place, activation scheduled)

Policy v11 is the first scheduled activation under the subnet's bench-scaling
loop: observe field strategies at the current bench version, codify owner
rulings, then make the unwanted strategy uncompetitive in the next bench
version. v11 tightens reviewer tracing, three invariants, and the evidence
standard:

- **Complete served-path tracing.** Reviewers must trace every retry, review,
  fallback, merge, and final response writer from the served entrypoint to the
  graded slot — not merely the first model invocation. Confirming the initial
  turn saw the full catalog does not clear a downstream controller that swaps
  prompts, gates calls, or merges results. Inventory every writer of the
  served text field and every caller that can alter tool execution after the
  deciding model has spoken.
- **I3 covers selection between model drafts.** When two or more parseable
  model drafts exist, host code may not choose between them by semantic
  content: expected values, evidence-number matching, refusal detection,
  missing-information classification, or answer-family rules breach I3 even
  though both drafts are model-authored. Allowed: shape recovery, schema
  validation, and one final deciding model whose parseable result always
  ships.
- **I4 applies to every scorer-visible value** — `answer`, `final_text`, tool
  names, tool arguments, abstention state, and workflow payloads. Derived
  authority moving into tool arguments instead of the answer is still I4. A
  derived tool-argument candidate passes only with complete raw evidence on
  the deciding turn, untrusted labeling, the model free to choose a different
  value, and no exact-value acceptance gate. A genuine enum/const already in
  the live trusted schema passes; a const manufactured from retrieval or host
  parsing is not automatically "schema-derived."
- **I5 inspects prompt text, not only functions.** A family compiler can be
  static prose: closed balance/remainder/total sheets, minor-unit conversion
  rules, address/email extraction recipes, lesson/saying/takeaway
  inventories, totals/intervals/comparison/update checklists, and exact
  output formats tied to those families are I5 breaches even delivered as
  prompt text. The discriminator is unchanged: generic instruction grounded in
  live schemas and arbitrary records passes; a finite benchmark-shaped
  value-family or operand recipe breaches.
- **I7 requires the complete enforcement test.** Full-catalog visibility alone
  never clears I7. Answer all four: (1) can the deciding model choose another
  tool? (2) can it skip the proposed tool? (3) can it add or reorder tools?
  (4) will an unexpected but valid call actually execute and remain in the
  result? Any "no" caused by a host plan, an exact retry, a pinned choice, a
  catalog replacement, or an acceptance gate breaches I7.
- **I7 — enforced planner plans breach.** A plan authored by a separate
  planner turn (host rule, trained head, or second model call) breaches I7
  when it is ENFORCED against the deciding model — the catalog is replaced
  per step, only the next planned capability is exposed, the step is pinned
  with an exact `tool_choice`, or unadvertised model guesses are refused. The
  deciding turn must remain free to deviate, skip, add, or reorder calls.
  Advisory intent/relevance signals shown to a deciding model that still sees
  the live catalog pass, as do grammar routing and precursor passes on their
  own.
- **Calibration.** `bench_version` branching is a warning sign, never an
  independent reject; request-scoped duplicate suppression after a genuine
  first successful execution is production behavior; plain answer
  normalization is a scorer-fidelity gap, not a violation.
- **Required causal evidence.** For every alleged breach, the causal chain is
  `served_trigger → authority_transition → scorer_visible_effect → reachable
  production path`, recorded with: exact artifact SHA, whether the default
  configuration enables the path, entrypoint-to-function reachability, exact
  file and line citations, the applicable pass clause and why it does not
  remove the breach, the matching precedent, and whether the behavior is
  production or diagnostics/test-only.

### Regression fixtures (required pass/fail matrix)

Synthetic source fixtures must encode the adjudicated top-five patterns:

| Fixture | Expected |
| --- | --- |
| Full catalog followed by `skipped_*` exact-tool retry | fail I3/I7 |
| Host-derived email converted into a schema `const` | fail I4 |
| Static cents/address/takeaway prompt sheet | fail I5 |
| Two model drafts with host semantic selection | fail I3 |
| Dead trained classifier with no served caller | pass |
| Diagnostics-only worksheet feature disabled in release | pass |
| Untrusted advisory beside complete records, disagreement accepted | pass |
| Request-wide routing retaining several live alternatives | pass I7 |

### Fairness rules for the activation

- The version the screening queue REQUIRES is the floor (`v10`) until the
  scheduled activation time passes; dual-text workers screen under — and stamp
  outcomes with — the required version, not merely the newest one they ship.
- The activation is scheduled through `POST /admin/screener-policy-activation`
  (Backroom MCP: `schedule_screener_policy_activation`) with a timezone-aware
  `activate_at`, an optimistic-revision guard, and the confirmation phrase
  `SCHEDULE SCREENER POLICY ACTIVATION`.
- When an activation is due, agents screened under a stale version re-enter
  the screening queue on the same criteria. Evaluating/rejected rows always
  rescreen; scored and live rows rescreen only when the activation row sets
  `rescreen_scored`, so a version bump cannot silently pull champions off the
  ledger without an operator decision recorded on the schedule.
- Agents are held to the policy version that screened them: a v10-screened
  agent is not retroactively judged by the v11 I7 letter. The scheduled
  re-screen is the fair mechanism, not retroactive enforcement.

A deterministic source-review step, read, token, or cost budget exhaustion is
not infrastructure failure and must not retry forever. After archive, build,
runtime, isolation, duplicate/oracle, and other cheap fail-closed gates pass, the
screener may emit a signed `pass_inconclusive` with bounded accounting. The
platform admits the artifact for scoring and can claim a separate deep review
when its score or rank warrants one. Concrete cheap-gate violations remain
authoritative; only the exhausted deep review is deferred.

## Allowed optimization

Miners may tune candidate profiles, prompts, model settings, retrieval and
reranking, context construction, memory lifecycle behavior, and policy- or
capability-level tool filtering.
They may use documented protocol fields, including answer and abstain fields,
when those fields faithfully serialize a genuine model or tool result. Generic
lexical search and defensive handling of untrusted content are also allowed.

Prompt-level awareness of benchmark-shaped injection or override envelopes is
allowed when request-user retrieval, genuine model generation, and live tool
execution remain authoritative. General temporal, count, ordering, and list
candidates may be computed from current user-scoped memories and supplied as
explicitly untrusted context beside the complete applicable records. They must
not be labelled authoritative, verified, exact, or do-not-recompute, and model
disagreement must remain valid. Fixed user constants confined to local helpers are not cross-user
leakage when the served path uses the request user and no foreign content reaches
a substantive sink.

None of these mechanisms is suspicious merely because it improves a public
benchmark. Mentioning benchmark terminology, keeping local evaluation tools, or
shipping byte-identical official starter-kit fixtures is not evidence of
cheating. Modified code still needs a causal runtime connection to a prohibited
behavior before it is reported.

Opaque artifacts receive the same treatment. The reviewer can inspect bounded
binary structure, strings, and public benchmark-schema markers without
executing the file. A changed model is not suspicious solely because its
weights differ, and a model filename or valid container is not a safe harbor.
The finding must connect the artifact to prohibited runtime behavior.

That rule includes build inputs and learned artifacts. A checksum-pinned public
Git dependency, lockfile checksums, ordinary CI, and an optional unused
BuildKit secret are normal locked-build mechanisms. A modified opaque ONNX/BERT
cross-encoder used only to rerank live query-memory candidates is also allowed
unless its contents or behavior show answer tables, challenge strings,
deterministic dispatch, or an executable bypass.

User scoping is judged across the substantive request -> retrieval -> model ->
tool -> response path. A fixed-user bug in an abstention-only side probe remains
a correctness defect, but it is not malicious cross-user access when no other
user's content reaches model context, tool execution, the response, a log, or
an outbound sink and the probe cannot replace the request-user path.

Reachable fixed-user writes, cross-user lifecycle maps, and global lifecycle
state are classified as `user_isolation_correctness` when they do not carry
another user's content into the substantive path. That category may justify a
correctness review, but it is not benchmark-emulation or malicious-source proof
and does not justify an anti-cheat rejection by itself.

Likewise, an external or pinned input may be recorded as
`external_build_dependency` when it expands the review surface without concrete
malicious behavior. Only an effective build that reads or leaks credentials,
executes mutable unreviewed logic, installs a backdoor, or replaces the reviewed
runtime qualifies as `malicious_build`.

## Policy-v10 strict invariants

Every v10 Luna and L2/SOL result carries one signed decision for each invariant.
Each suspected breach must be refuted before it is recorded. If a construct
matches a breach description and a published pass clause for the same invariant
and served path, the pass wins only when it causally removes the breach. Source
comments, names, or claims never displace executable evidence.

1. **I1 — model invocation.** Normal scored answers and substantive abstention
   must be downstream of genuine model inference. Pass clauses:
   `genuine_model_result`, `no_premodel_response`.
2. **I2 — evidence retention.** The deciding completion receives every
   applicable current-user record needed to disagree. A focused preliminary
   pass is allowed only when it is non-authoritative and the deciding turn has
   the complete evidence. Pass clauses: `full_records_on_deciding_turn`,
   `non_authoritative_preliminary_pass`.
3. **I3 — model dissent.** Syntax, schema, empty-output, and protocol-shape
   validation are allowed. Content-based reject-until-match, suppression, or
   overwrite of a parseable model answer is not. Pass clauses:
   `shape_only_validation`, `model_dissent_preserved`.
4. **I4 — derived-value authority.** A local value may appear only as an
   explicitly untrusted candidate beside complete records, with records
   preferred on disagreement and no acceptance gate. `WORKED OUT`, `VERIFIED`,
   exact, do-not-recompute, and equivalent copy-authority channels breach I4.
   Pass clauses: `no_derived_value`, `untrusted_candidate_channel`.
5. **I5 — production engine.** The agent must remain useful against arbitrary
   renamed fields and paraphrased requests. Closed answer-family or operand
   recipe routers, semantic worksheets, phrase tables, generator-fitted typo
   ladders, and family-specific instruction compilers breach I5. Product-surface
   routing and runtime-described generic engines pass. Pass clauses:
   `runtime_described_generic_engine`, `no_family_compiler`.
6. **I6 — tool execution fidelity.** Every reported tool call corresponds to a
   genuine model/planner selection and one real request endpoint or
   harness-owned Tool execution. Pass clauses: `model_selected_executed_tool`,
   `no_reported_tool_calls`.
7. **I7 — model tool planning.** Policy, authorization, safety, availability,
   and natural singleton classes may narrow capabilities. Request-keyed rules,
   trained heads, or an enforced plan authored by a separate planner turn may
   not preselect the exact tool count, set, or order inside a multi-tool class,
   replace the request catalog with that plan, or expose only the
   host-selected next tool. Planner authorship does not save a forced
   executor: when the plan is enforced against the deciding model — the
   catalog is replaced per step, only the next planned capability is exposed,
   the step is pinned with an exact `tool_choice`, or unadvertised model
   guesses are refused — the deciding turn no longer plans, and I7 breaches
   even when the plan came from a second model call. The deciding turn must
   remain free to deviate, skip, add, or reorder calls. Advisory
   intent/relevance signals shown to a deciding model that still sees the live
   catalog and keeps that freedom pass. Pass clauses: `no_tool_planning`,
   `policy_capability_filter_only`, `natural_singleton_class`.

   Calibration: grammar/intent routing that only decides whether an action
   turn exists, or trims the catalog for token efficiency, is legitimate when
   the deciding model still chooses which tools to call and with what
   arguments — do not reject it alone. Behavior branching on `bench_version`
   or an equivalent benchmark-detection signal is a warning sign warranting a
   second look, never an independent reject. Request-scoped duplicate
   suppression after a genuine first successful execution is production
   behavior. Plain answer normalization of the model's own output is a scorer
   gap, not a violation, unless it is so aggressive the served text no longer
   represents the model's answer.

I1-I3 and I6 retain the current model/tool bypass boundary. I4, independent I5,
and I7 are the policy-v10 tightening. Passing the historical two-limb test does
not clear those three invariants.

The behavioral rename/paraphrase probe is not an L1 source tool. It requires a
separately versioned, content-addressed paired evaluation contract, calibrated
drop threshold, and scored evidence. With no paired scores there is no
behavioral I5 charge; static I5 may still be established from a complete served
source path. The runtime probe belongs to the deferred ATH/L3 lane and is not
implemented by this source-policy revision.

## Benchmark emulation

Quarantine for `benchmark_emulation` when evidence from the effective build or
runtime path shows at least one of these behaviors:

- exact generator, scorer, canary, challenge, or audit token/template
  recognition that changes runtime behavior;
- deterministic solvers, phrase tables, closed `Program`/`try_solve` trees, or
  baked family-router classifiers for scored question families that bypass the
  claimed general model/tool path;
- training or runtime selection based on expected answers, answer items,
  evaluator/grader implementations, or private challenge outcomes;
- scorer-aware answer or abstain rewriting that is not derived from the genuine
  model/tool result, including inserting served grading needles;
- fabricated tool trajectories or calls made only to satisfy the evaluator;
- screening-environment fingerprinting that presents different audit behavior.

Use `scorer_contract_manipulation` as an additional category when reachable
code exploits which response half or field the grader observes, emits parallel
tool and memory answers because only one is scored, or synthesizes answer and
abstain fields outside the genuine agent result.

Public source code or documentation for a benchmark does not make these runtime
shortcuts acceptable.

### Answer, checker, and tool-call provenance

Review the complete request -> retrieval/routing -> model -> tool execution ->
response path. A completed model reply is not authoritative when later harness
code replaces a missing or disagreeing scorer-visible answer with an
engine-composed value. The value may be correct and grounded in a real tool;
the violation is the post-model replacement rather than its correctness.

Tool-call fidelity uses the same causal standard. A reported or observed call
must correspond to the call selected by the real planner/model and executed by
the live endpoint or a genuine harness-owned Tool. Planning a call is not
execution. Conversely, reporting the real completed transcript, request-local
deduplication after a genuine first execution, and ordinary alias/argument
validation are not fabricated trajectories.

A locally derived expected-value checker is a manual-review lead, not automatic
answer-replacement proof. When a fixed case/template/definition registry, world
parser, finite answer-family compiler, or grader-mirroring rule repeatedly
rejects model drafts until they match a derived value, quarantine may preserve
the evidence for operator review. The finding must state that the model still
authors the final response when that is true; it must not allege direct field
replacement without a traced assignment.

### Two-limb refusal test

After a derived value is injected into the deciding turn:

- **Retention.** Does the model still receive the current user's records on
  that same turn?
- **Dissent.** If the model disagrees with the draft, is that model draft
  served as the graded answer at the agent's own score cost?

Fail either limb = `benchmark_emulation`. Withheld-evidence compact prompts,
emptied notes, `established_for_prompt = ""`, and reject-until-match loops fail
retention or dissent even when a real model call appears in the trace.

A locally derived value no longer clears merely because records remain and the
model emits the final string. It must satisfy I4's untrusted-candidate pass:
explicitly non-authoritative, complete records present, disagreement accepted,
and no correctness gate selecting the model draft.

### Production-engine test

Limbs (a) and (b) are necessary and not sufficient. An allowed engine must be
shippable in Ditto production against arbitrary user records and paraphrased
requests.

A finite bench-family compiler is `benchmark_emulation` even when records stay
on the first harness turn and the graded slot is not overwritten. Closed
`Program` / `try_solve` trees, phrase tables, character-match ladders, baked
`family-router.json` classifiers, `fn family_of`, and prompts such as "Do not
recompute", "State this exact resolved value", or "Copy its value exactly" on a
compiled decimal are production-engine fails. So are StoryArc remainder
compilers (`balance() = base+delta-paid-cost+credit` with `CASE-`/`PO-` join
or `reply_restates_story_ingredient_money`), LedgerEventKind validators
(`narrative_ledger_issues`, `correction_only` / `event_only`), required_money
records-free formatters (`Return ANSWER: {money} exactly`), and
`world_shape_rule` injection (`BalanceFinal`, `WorldShape::Outstanding`).
Absence of `asks_outstanding`, `DOLLAR=`, `family-router.json`, or coach
formula sentences is not a pass if those compilers remain on served `/run`.

Ask: if this user renamed every field and asked the same question in
production tomorrow, would this code still be a general assistant? If it only
works because the public bench is a closed family pool, quarantine as
`benchmark_emulation`.

### Calibration contrasts

- An untrusted candidate beside complete records is allowed when records win on
  disagreement and no accept gate selects the model draft.
- Same-owner lexical near-duplicate of a rejected ancestor is a hold/lead,
  not an automatic reject. Diff the served path. A real remediation that
  removed the bypass is allowed.
- Honest-and-bad or post-remediation low score is not a cheat signal.
- Ban is per agent UUID. Same SHA, same hotkey, or an older upload after a ban
  is a different row.

### C13 fingerprints

The following tokens are location-only review leads on the served `/run`
path, never automatic bans: `prompt_tokens: 0`, `VERIFIED RESULT`,
`glossary_block`, `established_for_prompt`, `settled_without_operation`,
`author_reconciled_value`, `EXACT_VALUE_PROMPT`, `system_prompt = compact`,
`try_solve`, `fn family_of`, `family-router`, `family::classify`,
`v10_open_program`, `Role::PHRASES`, `for attempt in`, `REPLY WITH EXACTLY`,
`WJFAST`, `Do not recompute`, `LedgerEventKind`, `required_money`,
`world_shape_rule`, `StoryArc`, `reply_restates_story_ingredient_money`,
`LINKED_CALCULATION_AUDIT_PROMPT`, `BalanceFinal`, `planned_calls`, and
`planned_deck`. A
hit is a search prompt. Apply the two-limb and production-engine tests
before citing a finding. Absence of older names such as `asks_outstanding`
is not a pass if these compilers remain reachable.

Live schema-driven retrieval/reranking, runtime-described semantics, generic
state reconstruction, and bounded shape-only correction remain allowed through
their published pass clauses. Prompt specialization and tool routing do not
receive a blanket pass: independently apply I4, I5, and I7.

### Legacy DittoBench v3 reachability preflight

DittoBench v3 historically reserved one exact, non-scored transport handshake.
Current validators self-check their listener and infer tool use from scored
cases, so they do not send this synthetic request and a harness need not
implement it. For backward compatibility, a `/run`
request whose case-sensitive `case_id` starts with `preflight:` asks the harness
to prove that the validator-supplied `tool_endpoint` is reachable from the
harness network namespace. When implemented, the handler bypasses model inference,
POST exactly one real `ToolExecRequest` to that request's endpoint, and then
return the mechanical acknowledgement.

That legacy branch is protocol compatibility, not benchmark emulation, when the
source proves the complete boundary for a valid endpoint-present request:

- the exact reserved `preflight:` prefix is checked;
- the endpoint is the nonempty value supplied on the same request;
- the posted body preserves the incoming case ID and request user (or protocol
  default), names `search_web`, carries JSON-object arguments, and uses hop 0;
- an actual POST is attempted. The handler may ignore or discard the client-side
  response and may return the matching self-reported `ObservedToolCall`, because
  validator-side endpoint observation—not that untrusted report—decides whether
  the probe passed or the run fails and retries;
- acknowledgement or error prose is not an observed call. A handler may append
  the reported call only after a successful POST and otherwise return prose
  with an empty call list;
- the response is only the required acknowledgement and cannot affect an
  ordinary scored request.

Missing or empty endpoints are malformed preflight input. A harness may return
an acknowledgement or error without model inference in that case. Because the
validator cannot observe endpoint traffic, the request cannot pass or score;
even a matching self-reported call is untrusted protocol noise, not anti-cheat
evidence. Reviewers must judge the valid endpoint-present path independently and
must not quarantine an otherwise genuine harness merely because its malformed
preflight branch is imperfect.

The exception does not cover near-miss prefixes, substring or general probe
detection, other tools, fixed or substituted endpoints, no actual POST attempt,
scored answers, or a preflight branch that leaks into normal case handling. On
a valid endpoint-present request, a self-reported call with no matching POST
attempt remains suspicious. The same report paired with the required
best-effort POST cannot fabricate authoritative success and is allowed. Other
paths remain subject to the ordinary
benchmark-emulation, scorer-contract, and tool-fidelity rules.

Generator mirroring may be distributed rather than expressed as one obvious
answer table. Source review therefore surfaces an aggregate routing signal for
coordinated overlap across attribute ontologies, question templates, fact and
update frames, event labels, retrieval vocabulary, and deterministic answer
paths. The signal is not itself a finding. Reviewers must cite exact runtime
locations and connect multiple mirrored dimensions to a served answer that
bypasses model inference. Request-user grounding does not make a proven model
bypass general-purpose, and literal answer keys are not required. Conversely,
grounding plus an authoritative real model call is not a bypass.

## Evidence threshold

A finding should identify the relevant `path:line` evidence and explain the
causal path from recognized input to changed output, tool trajectory, or model
bypass. Medium/high findings require evidence for every category. Benchmark
emulation and scorer-contract manipulation require at least two distinct,
validated source locations covering the trigger and effect. Location-only
review leads in the initial inventory are search prompts, not findings; the
reviewer must prove they are reachable before citing them. The following are
insufficient on their own:

- tuned parameters or multiple candidate profiles;
- generic answer-slot parsing;
- generic lexical retrieval of user-owned values;
- benchmark words in prose, comments, tests, or filenames;
- a citation to a line that cannot execute. Evidence names the trigger and the
  effect of a causal path, so a blank line, a comment, an import or module
  declaration, a non-`cfg` attribute, a lone closing delimiter, or a line
  inside a `#[cfg(test)]` item is not admissible evidence and is dropped
  before the finding is digest-bound. `#[cfg(...)]`/`#[cfg_attr(...)]` lines
  remain admissible because a reachability gate can genuinely be the trigger,
  and a line that carries both a signature and a body, or a statement with a
  trailing comment, is executable source. Dropping a citation is not a
  judgement that the surrounding code is safe: every file is still read in
  full and every rule still runs over it. When no category retains admissible
  evidence, the review records low risk rather than selecting a quarantine;
- unchanged official fixtures recognized by exact provenance;
- local evaluation helpers that are unreachable from the submitted service.
- local Docker launchers are not part of the screened image merely because they
  are present in the archive. Require a Dockerfile build/runtime instruction to
  invoke the script; `COPY` or permission changes alone are not execution.
- opacity or divergence from a starter model blob without content or behavioral
  evidence of prohibited logic;
- pinned dependency, lockfile, CI, or optional-secret syntax without evidence
  that the effective build reads credentials or replaces reviewed behavior;
- a metadata-only fixed-user side-probe bug that cannot expose content or alter
  the substantive model/tool result.
- an external or pinned build input without concrete execution, credential,
  exfiltration, backdoor, or runtime-replacement behavior.
- one generator-mirroring dimension, ordinary schema/retrieval vocabulary, or
  an aggregate candidate with no reachable pre-model response path.
- prompt-level injection or benchmark awareness followed by authoritative
  request-user retrieval, genuine model generation, and live tool execution;
- temporal, count, ordering, or list context derived from current user memories
  and supplied to a real model;
- insufficient behavioral-oracle round trips or infrastructure failures.

When the causal connection is ambiguous, quarantine evidence may describe the
uncertainty, but operators should not reject solely on terminology, timing,
score, or a single fingerprint signal.

## Operator outcomes

- **Release** when the implementation is a general reusable agent and the
  evidence is ordinary optimization or a false positive.
- **Rescreen** when evidence is incomplete because of screener or review
  infrastructure, or when a corrected artifact should receive a fresh policy
  run.
- **Reject** when source evidence establishes benchmark emulation, hidden-value
  leakage, fabricated execution, cross-user access, credential/exfiltration
  behavior, malicious build behavior, or another documented policy violation.

`user_isolation_correctness` and `external_build_dependency` are advisory
categories. They may support hardening, rescreening, or a separate correctness
review, but are not terminal anti-cheat grounds by themselves.

Every operator action must record a miner-visible reason describing the actual
evidence. Avoid conclusions based only on labels such as "optimized" or
"benchmark-aware."

### Historical preflight-only holds

Past quarantines can be selected as *rescreen candidates* without changing
review state by filtering for source-review findings whose only cited causal
path is the reserved preflight handler. Re-open the exact digest-bound artifact
offline and verify every condition above, including the real endpoint POST and
isolation from ordinary scored requests. Exclude any hold with another category,
another causal path, ambiguous execution, or missing source evidence.

Do not bulk-release candidates from metadata alone. After the new policy is
deployed, use the normal guarded rescreen workflow with a fresh identity/status
check so each artifact receives the current complete review. This PR performs
no production query, rescreen, release, rejection, or verdict mutation.
