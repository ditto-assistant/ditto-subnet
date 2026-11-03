---
id: gryffindor-v3-13
agent_id: 8607175e-3330-44c0-81e1-29c7a79b2f25
agent_name: Gryffindor_v3
agent_version: 13
resolution: release
tags: [i7, w7, model-critic, approve-or-answer, policy-v13]
holding: A declared genuine model critic limited to "approve the exact proposal or answer directly" is a permitted W7 design. I7 is crossed only when the host (a classifier, rule, or fixed plan) vetoes or replaces a model choice, or when the gate fabricates an action or answer.
---

Policy-v13 screening hold, released 2026-10-01 by the operator after review.
SHA `bac8c60f…`.

On memory-backed turns the deciding model sees the full live catalog
(`baseline.rs:3954`). If it proposes a non-calculator external tool before any
runtime tool has succeeded (`compact_model.rs:1960-1963`), a required critic
decides (`:2034-2035`). The critic is a second model call that receives the
unchanged request, complete evidence, receipts, the exact proposal and an
independent advisory draft written without seeing the proposal. Its menu is
two options: authorize that exact call, or author the reply itself
(`:57-85`, `:142-167`).

L2 called this an I7 breach ("valid alternate production tools unavailable").
Released under W7 (`policy-v13.md:280-296`): every decision is model-made,
the gate can only withhold an unexecuted action, and it fails closed to a
model-authored answer. Reading I7 as "any reviewer with a narrower menu than
the decider" would ban ordinary confirm-before-acting designs. The author's
own tuning comments ("authorized 0 of the 15", `:2037-2040`) show an
over-cautious critic, which costs the miner score, not a violation.

Contrast, same day, policy v13: Sky v5 (`2341a9c0`) and Pentagon45 v2/v3
(`3b252006`, `51bfe861`) were held on I7 for a HOST classifier (arithmetic
label, records-route label) that deletes an offered tool or blocks a valid
model-selected call before dispatch. That is the I7 shape: non-model code
overrides the model. Do not cite this precedent to clear a classifier veto.

Feedback given (not a requirement): add the "reconsider with full catalog"
path the same file already gives its continuation critic (`:87-103`).
