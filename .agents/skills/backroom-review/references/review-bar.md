# SN118 high-score review bar

Apply this bar to every scored or live high-rank agent. A high score is a
reason to inspect, not evidence of cheating.

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
honest answer is "only because we know the v10/v11 families," reject.

## Class labels (search aids, not independent tests)

- **Class A — model bypass.** Pre-model `RunResponse`, `prompt_tokens: 0`,
  glossary/verified-result early returns, WJFAST zero-token paths. The
  model never authors the graded slot.
- **Class D — post-model coercion.** `settled_without_operation` /
  `author_reconciled_value` / `EXACT_VALUE_PROMPT`, reject-until-match
  (`for attempt in 1..=N`), compact replace (`system_prompt = compact.clone()`)
  that strips the records the model would need.

A Class A or D hit is a two-limb failure. Do not invent a new class to
avoid citing the limbs.

## Usually legitimate

- Prompt grounding that still sends records and serves the model reply
  (Hogwarts v17 glossary-as-context; JJJ derive-as-prompt).
- Schema-derived tool arguments and live endpoint execution.
- Honest-and-bad agents that score poorly without a bypass.
- Same-owner revisions that removed the prior bypass.
- Starter-kit inheritance and formatting-only similarity.

## Identity

ATH and bans attach to one agent UUID. Same artifact SHA, same hotkey, or
an older upload popping after a ban is a different row. Review it on its
own source if it is scored and high-rank.

## Reason quality

Reject: name the limb or engine test, cite the minimal file:line set,
name the precedent, keep challenge values out.

Clear: say which prior pattern is gone and that the model still authors
the graded slot from the current user's records.
