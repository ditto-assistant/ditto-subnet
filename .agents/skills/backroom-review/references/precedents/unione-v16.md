---
id: unione-v16
agent_id: c9557aef-52cf-4e65-bc15-884a8b6e30fa
agent_name: unione
agent_version: 16
resolution: reject
tags: [family-compiler, limb-b, renamed-tokens, host-overwrite]
holding: Same-owner restack of banned v14; host still overwrites the graded slot via calculated_money_answer.
---

Never-reviewed emission tail ~3.5h after unione v14 (`8b9ba0ad`) reject.
`deterministic_typed_recall_answer` / `final_source` identifiers were
gone. Served `/run` still wrote
`result.result.text = format!("ANSWER: {}", calculated_money_answer(...))`
and skipped the independent reviewer. Phrase table `net effect` / `up or
down` / `increase or decrease` ships `{increase|decrease}; {magnitude}`.

Limb (b) plus production-engine. Absence of `query::compile` is not a
pass. Renaming `final_source` is not a pass.

Inverse of unione v13 (`6545f688`), where `final_text = result.result.text`
and no host-served compiler remained.
