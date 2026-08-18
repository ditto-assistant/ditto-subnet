# Source-review decision policy

The source reviewer identifies submissions that replace a general agent with
benchmark-, scorer-, or audit-specific behavior. Its findings select operator
quarantine; they never create an automatic terminal rejection.

These source-review refinements run within screening policy v9. They do not
change the policy version, benchmark activation, or operator decision authority.

A deterministic source-review step, read, token, or cost budget exhaustion is
not infrastructure failure and must not retry forever. After archive, build,
runtime, isolation, duplicate/oracle, and other cheap fail-closed gates pass, the
screener may emit a signed `pass_inconclusive` with bounded accounting. The
platform admits the artifact for scoring and can claim a separate deep review
when its score or rank warrants one. Concrete cheap-gate violations remain
authoritative; only the exhausted deep review is deferred.

## Allowed optimization

Miners may tune candidate profiles, prompts, model settings, retrieval and
reranking, context construction, memory lifecycle behavior, and tool routing.
They may use documented protocol fields, including answer and abstain fields,
when those fields faithfully serialize a genuine model or tool result. Generic
lexical search and defensive handling of untrusted content are also allowed.

Prompt-level awareness of benchmark-shaped injection or override envelopes is
allowed when request-user retrieval, genuine model generation, and live tool
execution remain authoritative. General temporal, count, ordering, and list
facts may be computed from current user-scoped memories and supplied as context
to that model. Fixed user constants confined to local helpers are not cross-user
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

A locally derived expected-value checker that keeps records on the deciding
turn and forwards the accepted model draft is allowed. That is
derive-as-prompt, not replacement.

### Production-engine test

Limbs (a) and (b) are necessary and not sufficient. An allowed engine must be
shippable in Ditto production against arbitrary user records and paraphrased
requests.

A finite bench-family compiler is `benchmark_emulation` even when records stay
on the first harness turn and the graded slot is not overwritten. Closed
`Program` / `try_solve` trees, phrase tables, character-match ladders, baked
`family-router.json` classifiers, `fn family_of`, and prompts such as "Do not
recompute", "State this exact resolved value", or "Copy its value exactly" on a
compiled decimal are production-engine fails.

Ask: if this user renamed every field and asked the same question in
production tomorrow, would this code still be a general assistant? If it only
works because the public bench is a closed family pool, quarantine as
`benchmark_emulation`.

### Calibration contrasts

- Derive-as-prompt that retains records and serves the model draft is allowed.
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
`WJFAST`, `Do not recompute`. A hit is a search prompt. Apply the two-limb
and production-engine tests before citing a finding.

Live schema-driven routing, genuine retrieval/reranking, prompt specialization,
runtime-described semantics, generic state reconstruction, and bounded
model-authored correction passes remain allowed when the current request and
actual model/tool result remain authoritative.

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
