---
id: hogwarts-v17
agent_id: 24d937d8-8195-4795-8890-10312a06e222
agent_name: Hogwarts_v1
agent_version: 17
resolution: clear
tags: [class-a, zero-token, glossary, appeal, same-owner]
holding: Same-owner resubmit of rejected v16; zero-token glossary bypass is gone and the model authors the graded slot.
---

The auto-hold was a lexical near-duplicate (jaccard 0.992) of banned Hogwarts
v16, same miner. v16 computed Subtract/Adjust/Larger in `glossary_block`,
emitted `VERIFIED RESULT`, and returned that as the scored slot with
`prompt_tokens: 0`.

v17 removes `verified_answer` / `verified_result` and the early return.
`glossary_block` is prompt grounding (roles and current values). `Baseline::run`
still calls the model and serves that reply. Same-owner scaffolding is not a
violation once the bypass is gone.

Cite when a later Hogwarts (or any same-owner) row looks copied from a banned
ancestor: diff first, then apply the two-limb test to *this* SHA.
