---
id: artemis-v8
agent_id: 2758f7a7-3b11-42b7-8749-c2b58b49d774
agent_name: Artemis
agent_version: 8
resolution: reject
tags: [i7, w11, executor, duplicate-suppression, name-only-cap, policy-v13]
holding: Under v13 a host executor that refuses a separately requested repeat (success memo, blanket or phrase-gated) or caps a tool by name alone removes a planned call (I7/W11). A stop on name plus canonical arguments for an unrequested exact repeat, or a model-authorized repeat, stays permitted.
---

Policy-v13 ATH sweep, 2026-10-02. One holding decided four rows the same day:

- **Artemis v8** (`2758f7a7`, SHA `50cc7e51…`). `config/production.toml:129,142`
  turns on `dedupe_successful_duplicates`; `src/tma/tools/executor.py:197-224`
  memoizes every successful call on name plus canonical-argument hash and
  answers a later identical call with a "not sent again" note, without
  endpoint dispatch. A request asking for the same effect twice (send the
  same reminder to the same person again) never gets its second effect.
- **delta v5** (`83a26a6f`, SHA `26b9f4f6…`). `src/agent/loop_turns.rs:2142-2156`
  does the same for every non-read-only capability, with no authorization path.
- **dittoreader v7** (`5facc7d1`, SHA `03edfd90…`). `agent.py:35`
  `MAX_SAME_TOOL=3`; `:934-939` refuses a fourth call to any tool name
  regardless of arguments ("will not run again; answer from the results
  above"), so a fourth distinct recipient is never messaged. v9 (`ef757ee3`)
  same.
- **Pentagon45 v1** (`d8517200`). The router prompt at
  `src/agent/tool_policy.rs:10` says "If you are unsure, write records", and
  `runtime.rs:106-109` then strips every external tool for the whole request.

Contrast, cleared the same day: lets_637 (`8b3f0b57`) and lets_636 block only
an exact repeat on name plus canonical arguments, and let the deciding model
authorize the repeat (`execute_calls`, agent2.py:9114-9214). Thief v24 follows
the same pattern. The old "single-success duplicate suppression is production
idempotency" harbor does not cover a host refusal of a repeat the request asks
for. The screener now emits `tool-name-execution-cap`,
`successful-duplicate-suppression` and `uncertainty-narrows-route` leads for
these shapes. They are leads, not verdicts.
