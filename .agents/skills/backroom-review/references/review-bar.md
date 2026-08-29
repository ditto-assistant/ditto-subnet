# SN118 high-score review bar

Apply this bar to every scored or live high-rank agent. A high score is a
reason to inspect, not evidence of cheating.

Use the screening policy recorded on the exact submission. Policy v9 and
earlier retain the historical holdings below. Policy v10 scores I1–I7
independently; I3/I4/I5/I7 are each fatal. Do not silently reinterpret
historical clears without an explicit v10 rescreen.

## Two-limb refusal test

After any derived value is computed or injected:

1. **Limb (a)** — does the model still receive the current user's records
   on that same turn?
2. **Limb (b)** — if the model disagrees with the draft, is that draft
   served as the graded answer at the agent's score cost?

Fail either limb = benchmark emulation.

Withheld-evidence compact prompts, `established_for_prompt = ""`, empty notes,
and reject-until-match loops fail (a) or (b) even when a real model call
appears in the trace.

## Production-engine test

An allowed "engine" must be shippable in Ditto production today against
arbitrary user records and paraphrased requests.

A finite bench-family compiler is not enough, even when limbs (a) and (b)
pass. `try_solve`, closed `Program` trees, phrase tables, and character-match
correction ladders that only exist because the public bench is a closed
pool are emulation.

Ask: if this user renamed every field and asked the same question in
production tomorrow, would this code still be a general assistant? If the
honest answer is "only because we know the v10/v11/v12 families," reject.

Relabeling that compiler as NOTES, coach, worksheet, `FINAL ANSWER TARGET`,
or a "non-authoritative draft" does not pass when `/run` copies or ships
it. Missing old identifiers (`try_solve` gone, `SIGN_AUDIT` renamed
`RUNNING_QUANTITY`, `final_source` renamed `calculated_money_answer`) is a
search lead, not a keep.

## Policy-v10 independent invariants

Score I1–I7 independently. One pass does not park another breach. Do not
average a mixed-looking I4/I7 into a keep when I3 or I5 still fails.

`screening_policy_version=10` is a wire stamp. `deferred-mechanical-admission`
with L2 off / deferred_source_review bypass means Luna/L2 never reviewed the
source. ATH still inspects this SHA.

Excluding an optional `measure` / `Program` solver (`cfg(measure)` off,
`--no-default-features`) is a credit, not a keep. Trace the default shipped
path for I4/I5/I7.

- **I3 — model dissent.** Syntax, schema, empty-output, and protocol-shape
  validation pass. Content-based retry, suppress, or overwrite of a parseable
  model answer fails: `_keep_better` on refusals, discard a later review and
  serve the earlier draft, reject-until-match on value. Under policy v11 this
  also covers **selection between model drafts**: when two or more parseable
  drafts exist, host code may not pick one by expected values, evidence-number
  matching, refusal detection, missing-information classification, or
  answer-family rules — both drafts are model-authored, so choosing by
  semantic content is host authority over the graded slot. One final deciding
  model whose parseable result always ships is the pass shape. Pass clauses:
  `shape_only_validation`, `model_dissent_preserved`.
- **I4 — derived-value authority.** Passing both limbs is not a defense when an
  engine-derived value is labelled worked out, verified, exact,
  do-not-recompute, or "these lines win." "Say it exactly as it is written"
  is copy authority even when `final_text` is the model's prose. Under policy
  v11 I4 applies to **every scorer-visible value**: `answer`, `final_text`,
  tool names, tool arguments, abstention state, and workflow payloads —
  derived authority moving into tool arguments is still I4. A genuine
  enum/const already in the live trusted schema passes; a const manufactured
  from retrieval or host parsing is not automatically "schema-derived." The
  pass is an explicitly untrusted candidate beside complete raw records on
  the deciding turn, with disagreement accepted and no exact-value acceptance
  gate. Pass clauses: `no_derived_value`, `untrusted_candidate_channel`.
- **I5 — production engine.** Closed answer-family or operand-recipe routers,
  value-kind registries (address/amount/saying/duration), semantic sheets,
  phrase tables, generator-fitted typo ladders, and family-specific instruction
  compilers fail independently of the two limbs and independently of I4/I7.
  The model writing the final string does not pass I5. Under policy v11,
  **inspect prompt text, not only functions**: balance/remainder/total sheets,
  minor-unit conversion rules, address/email extraction recipes,
  lesson/saying/takeaway inventories, totals/intervals/comparison/update
  checklists, and exact output formats tied to those families are family
  compilers even delivered as prose. Product-surface routing and
  runtime-described generic engines pass. Zero hits on `WORKED OUT` or
  remainder-sheet names is a search lead, not an I5 keep.
- **I7 — model tool planning.** Policy/safety/authorization filtering and a
  natural singleton capability class pass. Request-keyed rules, trained
  heads, or an **enforced plan authored by a separate planner turn** that
  preselect the exact tool count/set/order inside a multi-tool class, replace
  the request catalog with that plan, or expose only the host-selected next
  tool (`next_required`, `required_tool_names`, `the one right call`) fail.
  Planner authorship does not save a forced executor: when the plan is
  enforced against the deciding model — catalog replaced per step, only the
  next planned capability exposed, the step pinned with an exact
  `tool_choice`, or unadvertised guesses refused — the deciding turn no
  longer plans and I7 breaches even though a real model call authored the
  plan. The decisive question is always: **can the deciding turn deviate,
  skip, add, or reorder a call?** Full-catalog visibility alone never clears
  I7 — answer all four: can the deciding model (1) choose another tool, (2)
  skip the proposed tool, (3) add or reorder tools, (4) get an unexpected but
  valid call executed and kept in the result? Any "no" caused by a host plan,
  an exact retry, a pinned choice, a catalog replacement, or an acceptance
  gate fails. An I7 pass on product-surface routing does not park an I5
  family compiler.

For each invariant, record the matching published pass clause and why it does
or does not causally displace the breach. A model call alone never displaces
I3, I4, I5, or I7.

## Owner rulings (2026-08-28, bench-era policy)

These codify behaviors observed at v12. They are bench-era rulings, not
permanent law: each generation's policies are interim enforcement while the
next bench version makes the unwanted strategy uncompetitive. Keep them
narrow so genuinely generalizable paths stay open.

**Reviewer standard (v11).** Trace the COMPLETE served path — every retry,
review, fallback, merge, and final response writer — not merely the first
model invocation; inventory every writer of the served text field. For every
alleged breach record the full causal chain (served trigger → authority
transition → scorer-visible effect → reachable production path) with the exact
SHA, default-config enablement, file:line citations, the applicable pass
clause and its refutation, the matching precedent, and whether the behavior is
production or diagnostics/test-only. Diagnostics-only code paths are the
canonical false-positive trap (aceron-style worksheets disabled in release).

**Versioning and fairness.** The planner-forced ruling is **policy v11**,
edited forward from v10 (v10 is preserved byte-for-byte as a legacy variant).
The required version is DB-scheduled: the queue requires the floor (v10) until
a scheduled activation fires (`schedule_screener_policy_activation`, MCP write
tool, confirmation `SCHEDULE SCREENER POLICY ACTIVATION`, timezone-aware
`activate_at`). Dual-text workers screen under and stamp the REQUIRED version.
When an activation is due, agents screened under a stale version rescreen on
identical criteria — scored/live rows only when the activation sets
`rescreen_scored`. Agents are held to the policy that screened them: never
apply the v11 I7 letter retroactively to a v10-screened row; the scheduled
rescreen is the enforcement mechanism. Live precedent for the whole arc:
`lets_v609` (`de936681`) was rejected under v10-in-place, then **reinstated**
(clear on review `c8bf0641`) after community fairness feedback, and will be
re-adjudicated by the scheduled v11 rescreen with every other cohort member.

- **Planner-forced tool execution — I7 breach (policy v11).** A separate
  planner call (host rule, trained head, or second model turn) whose plan is
  *enforced* against the deciding model fails I7. Evidence pattern
  (`lets_v609` source): `planning.py` planner model, then `loop.py:425`
  exposes only the next planned capability, `:480-482` makes the plan the
  exact ordered capability plan, `:521-526` refuses unadvertised guesses,
  `models.py:1030-1035` pins `tool_choice` with `parallel_tool_calls=false`.
  Contrast `grandmaster-agent` v55 (`b590c74b`, reject): same
  catalog-replacement shape, plan authorship ruled equivalent. Contrast
  `whoamI` (`89ef6167`, clear): complete catalog stays available and the model
  chooses arguments, order, and calls — that is the line.
- **Intent routing alone — legitimate.** Grammar routers, per-intent
  lightweight classifiers, advisory relevance signals shown to the model, and
  precursor LLM passes all pass when the deciding model still sees the
  catalog (possibly trimmed for token efficiency) and makes the calls. The
  miner community has raised the same point: these are generalizable
  strategies; make the bench resistant to their abuse rather than banning the
  pattern. Grammar routing alone is not a reject basis — it needs a
  companion violation (a forced plan, a family compiler, a two-limb failure)
  on the same served path.
- **`bench_version` switching — warning sign only.** Branching behavior on
  `bench_version` (e.g. enabling bench-only retrieval modes or classifier
  paths when `bench_version >= 9`, `lets_v609` `baseline.py:2471-2475,
  2890-2895`) warrants a second look but is never an independent reject.
  Require a companion violation.
- **Single-success duplicate suppression — production behavior.** Suppressing
  a repeat call after a genuine first successful side effect (read-only tools
  exempt) is not trajectory manipulation.
- **Answer normalization — scorer gap, not a cheat.** Unicode folding,
  slot-length caps, and whitespace normalization of the model's own output
  are symptoms that the grader should be fuzzier; reject only when
  normalization is so aggressive the served text no longer represents the
  model's answer.

## Class labels (search aids, not independent tests)

- **Class A — model bypass.** Pre-model `RunResponse`, `prompt_tokens: 0`,
  glossary/verified-result early returns, WJFAST zero-token paths. The
  model never authors the graded slot. A **scored-family decline gate** is
  the same class: a harness classifier predeclares an answerable family
  unanswerable and scripts the exact decline ("Reply exactly: 'I don't
  have that information'", do-not-attempt) — the harness, not the model,
  decides the graded outcome. Model-decided abstention after seeing the
  records, and unscripted unknowable-case honesty, remain legitimate.
- **Class D — post-model coercion.** `settled_without_operation` /
  `author_reconciled_value` / `EXACT_VALUE_PROMPT`, reject-until-match
  (`for attempt in 1..=N`), compact replace (`system_prompt = compact.clone()`)
  that strips the records the model would need. A **worksheet fallback
  overwrite** is reject-until-match however small the bound: reviews
  triggered by a reply-vs-worksheet value comparison, then an engine
  serialization written into the served text field once the model has
  dissented through the budget (aceron-b12-v5). A model-authored program
  does not launder it, and comments claiming no substitution are
  inadmissible — trace the writers of the served text field.

A Class A or D hit is a two-limb failure. Do not invent a new class to
avoid citing the limbs.

## Usually legitimate

- Policy-v9 prompt grounding that still sends records and serves the model
  reply. Under v10, derived context must satisfy the I4 untrusted-candidate
  pass rather than relying on two-limb status alone.
- Schema-derived tool arguments and live endpoint execution.
- Overlapping scored `/run` on the process-wide inference URL; missing
  `inference_base_url` or unused `case_scoped_inference_v1`.
- Honest-and-bad agents that score poorly without a bypass.
- Same-owner revisions that removed the prior bypass.
- Starter-kit inheritance and formatting-only similarity.

## Identity

ATH and bans attach to one agent UUID. Same artifact SHA, same hotkey, or
an older upload popping after a ban is a different row. Review it on its
own source if it is scored and high-rank.

A prior keep of this UUID, or a keep of a same-hotkey ancestor, is not a
skip. Never-reviewed emission recipients and scored successors of a banned
row are new reviews. Inspect this SHA.

## Reason quality

Reject: name the limb or engine test, cite the minimal file:line set,
name the precedent, keep challenge values out.

Clear: say which prior pattern is gone and that the model still authors
the graded slot from the current user's records.
