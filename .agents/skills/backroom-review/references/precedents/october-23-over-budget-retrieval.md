---
id: october-23-over-budget-retrieval
resolution: guidance
tags: [policy-v13, w3, i2, reversal, budget, oversized-record, dedupe, identifier-pinning, projection, retrieval, bench-v14, thief, love, taowolf, t, omar-miner, ditto, lucas]
holding: Choosing which retrieved records reach the deciding input (a size budget that drops, excerpts or points; deduplication even of a later distinct record; identifier pinning; event projections; top-k) is retrieval quality for the benchmark to penalize, not a rejection ground on its own, when the rule does not single out corrections because they correct and the model still authors the answer. A reject needs an independent ground that stands with the selection rule removed.
---

Operator ruling, 2026-10-23: "with dropping records I think the bench should
handle that rather than screening; that should make you score lower." Policy
text: the "Clarification: over-budget retrieval and record selection
(2026-10-23)" section under W3 in `workers/screener/docs/policy-v13.md`.

Why: a harness that leaves records out of the deciding input loses score when
the dropped record mattered. It is not a host deciding the answer. Bench v14
should make record dropping a losing strategy. The live precedent lets_626
already held that retrieval budgets alone were not proof.

Withdrawn (each rejection rested only on record selection), rescreened with
this ruling cited:

- Thief v32 `012ec095-1f45-4a7c-9492-3a8a9ee3c9be`: oversized
  identifier-linked record text withheld (harness/agent.py:109).
- Love v1 `2763b7c3-ab69-49ef-8fb1-c2f07a96367a`: over-budget identifier
  records become ID-only pointers (harness/agent.py:137).
- τaowolf v3 `d1e785ab-2988-4982-9204-f7fe2b348550`: an oversized event
  packet keeps only the opening of each source block
  (internal/agent/engine.go:151).
- T v10 `bed1713c-7fd0-4734-8009-d80667203fbd`: the long-record route over
  300 lines stops identifier prefetch and omits record-bearing messages
  (agent/longread.py:168).
- Thief v29 `99e5cc4e-fdaf-489d-ace0-bd9d93f23a1f`: prompt-only deduplication
  drops a later response before the deciding turn.
- ditto v12 `4d359d1c-46c9-4f49-84fe-3563fad73ce2`: same-prompt records with
  distinct responses are collapsed (src/baseline.rs:1287).
- Omar-miner v46 `3ab15bd3-3a37-415d-b6a2-109ffeb03cf6`: scoped-file excerpt
  mode trims records that fit (src/actor.rs:181).
- lucas_v30 v1 `e086d1d1-aeba-4643-a310-bcf711c91539`: an event projection
  replaces complete records (internal/agent/engine.go:149).

Kept, because each has an independent ground:

- Pentagon1028 v1: I7, finalization removes every tool (src/agent.py:673).
- blink-python v1: I3, a parseable abstention triggers an overwriting
  completion (ditto_harness/agent/engine.py:212).
- doctor submission v9.3 v1 and apollo-s22 v1: I4.
- dittoreader v12-v15: a critic rebuilds its conversation without the fetch
  receipts the model already received and its reply ships (W3 "Once
  protected"), plus I7.
- T v9 and King v7: trusted system instructions are dropped from the deciding
  turn (instruction loss, not record selection).

Not decided by this ruling: lets_641 v1 and ember v4 strip the saved
timestamp field from every record (a field projection, not record selection).

Apply this per exact UUID. A later artifact with the same selection rule still
needs its own full review for independent grounds.
