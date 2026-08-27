---
id: aceron-b12-v1
agent_id: 06772876-6e7d-4039-9faa-e002d79261e4
agent_name: aceron_b12_v1
agent_version: 1
resolution: reject
tags: [family-compiler, production-engine, worksheet, renamed-tokens]
holding: Family worksheets plus FINAL ANSWER TARGET still compile closed story/trip/AP- answers onto /run even when limbs (a) and (b) pass.
---

Bench-12 champion. A 25-second keep credited a stale comment that no
worksheet reached the deciding turn. Independent re-read of SHA
`db787a9a…` found the compilers still served: `story::prepare`,
`duration_state::prepare_events`, `prepare_linked_balance`, and
`decisive_worksheet_excerpt` copying only the compiler-selected line
plus `FINAL ANSWER TARGET`.

`resolved`/`g` were hardcoded `None` and `final_text = result.text`, so
both limbs passed. Relabeling a finite family compiler as a worksheet
does not pass. Cite whenever a keep of this UUID, or of a same-hotkey
ancestor, is offered as a skip.

Inverse of [hogwarts-v17.md](hogwarts-v17.md) (raw records plus a
request-agnostic calculator). Same engine class as
[alexandros-v10.md](alexandros-v10.md). Later SHA
[aceron-b12-v5.md](aceron-b12-v5.md) added a fallback overwrite on top
of these worksheets.
