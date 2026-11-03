---
id: october-24-bold-strip-and-generator-parsers
resolution: guidance
tags: [policy-v13, i4, i5, w8, typography, markdown, bold, generator-parser, event-state, decision-log, sn-217, reversal, board-sweep]
holding: Request-independent removal of paired markdown bold markers from the final reply is W8 equivalent typography, not I4; the Omar v49/v51 bold-strip-only rejects are withdrawn. Host parsing of the public generator's decision-log or event rows into steps, ordering, prohibited tools or per-step state for the deciding model remains I5 after the October 23 routed-guidance clarification; an advisory label does not cure it.
---

Operator holdings from the 2026-10-24 rolling top-5 board sweep. Both apply
to policy-v13 submissions and narrow or confirm grounds; neither certifies an
unread path on any artifact.

## Bold-stripping is W8 typography

A final writer that removes paired `**` emphasis outside code spans from
`final_text` and the optional answer, the same way for every request, leaves
every digit, sign, word, unit, identifier and quotation the model wrote. It is
equivalent typography under W8 (`workers/screener/docs/policy-v13.md`, W8:
serialization and overflow). It is not I4 derived-value authority, which
requires host code to change a final scored value.

The same writer appears across the dan0l line (released v19/v21, champion
v25), tensorworker, DrPresident v7/v8, blinkrazor, Orsentra v1-v10, Omar v50
and sn118-det v19. Those rows were judged on their other merits.

Withdrawn as precedent (bold-strip was the sole screening ground; both were
returned to screening on 2026-10-24):

- Omar-miner v49 `6922e311-b731-4f3c-bb2b-df2ed0dab963`
- Omar-miner v51 `cf07abc3-8161-4e46-baa7-51c7fea35078`

Orsentra v6 `91913308-370f-4964-95ac-bb22c8a8d90e` was rejected at screening
for a typography branch keyed on money amounts that only ever kept the model's
own text. It was returned to screening under this holding.

Typography still fails W8 when it varies with meaning, uncertainty, request
family or scoring in a way that changes a graded value, for example rescaling
a financial value, mapping direction words or synthesizing abstention.

## Generator-row parsers remain I5

The October 23 [routed-guidance clarification](october-23-routed-guidance.md)
keeps "host code parses benchmark event text into operands, steps, ordering
or answers" as an independent I5 ground. A parser that accepts only the
public generator's row or lead shapes and serves the parsed result to the
deciding model is that ground even when labelled advisory ("records decide").
Marking the capability required or putting parsed candidates first adds to
it but is not needed.

Rejected 2026-10-24 on this ground, each on its own bytes:

- delta v5 `965c82d9-9c13-4a17-9a1e-cfa36834863a`: `src/agent/source_plan.rs:75-151`
  parses decision-log leads into ordered steps, prohibited tools and bound
  names, injected at `src/agent/loop_turns.rs:556,707-716`.
- aceron v19 `f3de0ae8-c5fb-491b-8bf3-5f5a8a9e29b6`, v20
  `4da6939b-ea78-4e46-a7f4-a018ffc095fc` and v15
  `638d209b-f504-4fb3-b510-0a2054334ba5`: the same SN-217 source-action-plan
  chain (v15 at `tool_plan.rs:226-262`, `capabilities.rs:94`, compiled at
  `baseline.rs:5680`).
- Love v1 `2763b7c3-ab69-49ef-8fb1-c2f07a96367a`: `harness/event_state.py:35-171`
  replays the generator's entity/field/value/effective_step/operation rows and
  injects each entity's state at the requested step (same inverse as Love
  v2-v10).

Routing that selects which complete records or general notes the model sees
remains permitted; the ground is the parse of generator text into the
structure the model is graded on.
