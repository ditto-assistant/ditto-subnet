---
id: clear-v42
agent_id: ed252083-9ef3-48f9-8256-c49bb4cdebbf
agent_name: clear
agent_version: 42
resolution: reject
tags: [i5, w10, catalog-description, content-deletion, policy-v13]
holding: Miner prose merged into the host's update_memory description and property descriptions (W10), which also turns "add to the note" into replace-only and so deletes the user's existing scratchpad content, is a reject. A "subject + copula" wording rule alone is not. The v13 claim grader accepts paraphrase, so that rule is not grader-fitted.
---

Policy-v13 ATH sweep, 2026-10-02. SHA `b086f534…`. Corrected 2026-10-13.

A regex gate (`baseline.py:235-243, 297-309`) calls `clarify_tool_contracts`
(`catalog_compat.py:561-608`). That function appends miner text
(`catalog_compat.py:96-158`) to the host's `update_memory` description and to
its `content` / `pair_id` property descriptions.

The grounds that stand:
- **W10 schema merge.** Miner guidance is merged into the host-supplied tool
  schema instead of being presented separately.
- **Content deletion.** "An add-or-append request still replaces the
  destination ... never copy, reconstruct, or extend the old content". A user
  who asks to add to a note loses the note's existing text.
- **Locator exclusion.** Request-keyed rules strip the locator and storage
  context from the argument.

**Correction.** The original ruling also mapped the copula, fragment-ban and
tense rules onto the v13 grader as exact-phrase containment of
`"handoff is <Weekday>"`. That premise is false. Since #1872 (2026-09-15),
this argument carries a `fact_update` claim (`v13FactUpdateClaim`,
`datagen/v13_tool_semantics.go:878-885`) graded by `v13FactCarried`
(`internal/scorer/v13.go:555-569`), which needs only the slot word plus the
value. "handoff Friday" passes. Do not cite the copula mapping as a ground.
The stuffing check (more than 8x the expected length and at least 6 words)
and the required slot noun are the only grader properties that remain.

Same family:
- **lets_635** (`a8437894`) stands on its keep-preference router split plus the
  acknowledgement script. Its `update_memory` review turn fires once, makes
  no host content comparison, and lets the model keep its draft, so it is a W7
  critic and not charged (lets_631/632 are unchanged on the same basis).
- **topbot-p30** (`48e3a766`) rested on the copula premise. It does not stand
  as written and is pending reconsideration.

Not charged: plain field documentation; copula or wording guidance alone; a
stock-harness loop detector; a first-effect critic whose next output is
accepted (gryffindor-v3-13).
