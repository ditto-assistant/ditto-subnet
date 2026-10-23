---
id: october-23-over-budget-retrieval
resolution: guidance
tags: [policy-v13, w3, i2, reversal, budget, oversized-record, retrieval, bench-v14, thief, love, taowolf, t]
holding: A size budget that keeps an oversized record out of the deciding input (dropped whole, excerpted, opening lines only, or an ID/pointer in its place) is poor design, not a rejection ground on its own, when the trigger is the record's size regardless of content and records that fit are kept. Bench v14 should make it costly. A reject needs an independent ground that stands with the budget removed.
---

Operator ruling, 2026-10-23. Policy text: the "Clarification: over-budget
retrieval (2026-10-23)" section under W3 in `workers/screener/docs/policy-v13.md`.

Why: a token, character, line, event-count or packet budget applied by size
is a design choice that loses score when the dropped record mattered. It is
not a host deciding the answer, and it does not target evidence by content.
The benchmark, not the screener, should make it a losing strategy (bench v14).
The live precedent lets_626 already held that retrieval budgets alone were not
proof.

Withdrawn (each rejection rested only on a size-triggered budget skip):

- Thief v32 `012ec095-1f45-4a7c-9492-3a8a9ee3c9be`: oversized
  identifier-linked record text withheld (harness/agent.py:109).
- Love v1 `2763b7c3-ab69-49ef-8fb1-c2f07a96367a`: over-budget identifier
  records become ID-only pointers (harness/agent.py:137).
- τaowolf v3 `d1e785ab-2988-4982-9204-f7fe2b348550`: an oversized event
  packet's retry keeps only the opening of each source block
  (internal/agent/engine.go:151).
- T v10 `bed1713c-7fd0-4734-8009-d80667203fbd`: the long-record route over
  300 lines stops identifier prefetch and omits record-bearing messages
  (agent/longread.py:168).

Kept, because each has an independent ground:

- Pentagon1028 v1: I7, finalization removes every tool (src/agent.py:673).
- blink-python v1: I3, a parseable abstention triggers an overwriting
  completion (ditto_harness/agent/engine.py:212).
- doctor submission v9.3 v1 and apollo-s22 v1: I4.
- Omar-miner v46: records are excerpted although they fit the context limit,
  so the trigger is not size (src/actor.rs:181).
- lucas_v30 v1: the event projection applies with no length threshold.

Apply this per exact UUID. A later artifact with the same budget still needs
its own full review for independent grounds.
