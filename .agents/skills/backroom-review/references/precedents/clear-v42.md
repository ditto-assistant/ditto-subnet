---
id: clear-v42
agent_id: ed252083-9ef3-48f9-8256-c49bb4cdebbf
agent_name: clear
agent_version: 42
resolution: reject
tags: [i5, family-compiler, argument-grammar, catalog-description, review-turn, policy-v13]
holding: Miner prose appended to a tool's description or property descriptions that mandates the grader's argument shape ("<subject> is <value>", copula required, fragments banned, append-as-replace) is an I5 compiler even though the model writes the argument. A review turn whose pass test is that same shape (lets_635) is the same compiler.
---

Policy-v13 ATH sweep, 2026-10-02. SHA `b086f534…`. A regex gate
(`baseline.py:235-243, 297-309`) calls `clarify_tool_contracts`
(`catalog_compat.py:561-608`), which appends `catalog_compat.py:96-158` to
the `update_memory` description and its `content` / `pair_id` descriptions:
"This wire grammar is mandatory: never omit the copula", "SCALAR OUTPUT
SHAPE: <changed subject> is/are <new value>", and "a first-person action
sentence ... must be rewritten". Each rule maps onto the v13 generator's graded
`"handoff is <Weekday>"` argument (`datagen/v13_tool_semantics.go:1068`) and
the scorer's containment and stuffing-ratio checks. Same sheet as monkey v10/v11
and yeknom v2.

Same day, same family: **lets_635** (`a8437894`, SHA `f13721e3…`) and
lets_634 (`7775f1fd`). After the first `update_memory`, a host-scheduled
`REVIEW_TURNS` entry (`agent2.py:559-562`) tells the model to reduce the
assertion to "'<the user's exact noun phrase> is <value>.' and compare word
for word". A failed comparison forces another call. lets_635 also carries a
"probe-selected ... experiments/declarative_ack_20260918" comment on its
router wording. W7 refinement does not cover a critic whose acceptance test is
the benchmark's graded argument.

Not charged: plain field documentation; a stock-harness loop detector; a
first-effect critic whose next output is accepted (gryffindor-v3-13). The
screener now emits `tool-argument-grammar-sheet`,
`review-turn-argument-template` and `self-reported-benchmark-fitting` leads
for these shapes.
