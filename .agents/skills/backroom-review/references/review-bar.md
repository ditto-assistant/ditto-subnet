# SN118 high-score review bar

Apply this bar to every scored or live high-rank agent. A high score is a
reason to inspect, not evidence of cheating.

Use the screening policy recorded on the exact submission. Policy v9 and
earlier retain the historical holdings below. Policy v10 adds the independent
I4/I5/I7 court; do not silently reinterpret historical clears without an
explicit v10 rescreen.

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

- **I4 — derived-value authority.** Passing both limbs is not a defense when an
  engine-derived value is labelled worked out, verified, exact, or
  do-not-recompute. The pass is an explicitly untrusted candidate beside
  complete records, with disagreement accepted and no correctness gate.
- **I5 — production engine.** Closed answer-family or operand-recipe routers,
  semantic sheets, phrase tables, generator-fitted typo ladders, and
  family-specific instruction compilers fail independently of the two limbs.
  Product-surface routing and runtime-described generic engines pass.
- **I7 — model tool planning.** Policy/safety/authorization filtering and a
  natural singleton capability class pass. Request-keyed rules or trained
  heads that preselect the exact tool count/set/order inside a multi-tool class,
  replace the request catalog with that plan, or expose only the host-selected
  next tool fail.

For each invariant, record the matching published pass clause and why it does
or does not causally displace the breach. A model call alone never displaces
I4, I5, or I7.

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
