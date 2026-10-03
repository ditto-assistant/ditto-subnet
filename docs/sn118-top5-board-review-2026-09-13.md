# SN118 top-5 board review — 2026-09-13 (decision table)

Human-readable decision record for the 2026-09-13 SN118 top-5 board sweep. The
canonical machine-readable rulings live in
`apps/platform/ditto/tests/fixtures/ath_rulings_replay_2026-09-13.json`
(`source: docs/sn118-top5-board-review-2026-09-13.json`, board snapshot
`2026-09-13T16:30:56Z`, bench version 12). Every row was executed as an
`open`+`resolve` ATH reject through the rulings court on that date; this page
restates the decisions and their decisive grounds for operator reference.

Court for all five rows: ATH board (scored emission recipients). Policy for all
five rows: screening policy v12. Scope of every ruling: the exact agent UUID
and artifact SHA-256 only — no owner-wide, hotkey-wide, or lineage-wide action;
accepted scores, screening evidence, and audit history preserved in each case.

## Decisions

| # | Agent / version | Agent UUID | Artifact SHA-256 (prefix) | Hotkey (prefix) | Court | Policy / invariants failed | Decision | Decisive path | Precedents cited |
|---|---|---|---|---|---|---|---|---|---|
| 1 | lets_623 v1 | `c25489aa-faf0-46fc-8b06-8e2240a5ac01` | `58deab6ed98e…` | `5ECg59rv…` | ATH board | v12 — I5 (+ I7 companion) | **Reject** (supersedes 2026-09-11 clear) | `routing.py:291-330` phrase route → `baseline.py:1859-1861`, `:2083-2222` PREFERENCE_ADVISORY_GUIDANCE / ACKNOWLEDGEMENT_REVIEW_PROMPT (family compiler); `baseline.py:1195-1207` closed font/model vocabulary inside trusted tool schema; `loop.py:364-382` update_memory rewrite recipe; I7: `routing.py:357-394` phrase table → empty catalog, no endpoint fallback, scripted zero-tool ask | lets_625 v1, Pentagon v3, Diana v1/v2, yeknom v6, Omar v5, 55555 v3, lets_622 v1 |
| 2 | comeτ-2 v1 | `db0d4d25-dce7-4c7c-a49a-425beacb6c05` | `e46bc446bb79…` | `5C7aHALb…` | ATH board | v12 — I7, I3, I5 | **Reject** | I7: `optimizer.rs:3561-3684` declarative classifiers → `baseline.rs:3786-3802` `withhold_host_tools` → `:15132-15141` empty catalog → `single_tool_model.rs:145-149` `tool_choice=none`; I3: `baseline.rs:15765-15820` "dumpish" wording test discards a parseable model draft and re-asks; I5: `baseline.rs:14714-14717` DECLARATIVE PREFERENCE TURN prescribing the exact `ANSWER:` slot value | dittoagent v7/v13, NM v11, Arachne v24/v16, taokika_v9 v1, Clear v25, Paragon v20, Lucas_v10 v3, Diana v1/v2, TeaCUP v1, Alexandros-v12-01, red-dragon v65, aceron_v16 v3 |
| 3 | Pentagon v1 | `5064eb97-b179-4a37-8b5f-9fb38f55f7db` | `e3f5b6bb3bfc…` | `5GhCZSCZ…` | ATH board | v12 — I5 | **Reject** | `serve.rs:79` ACTION_GROUNDING_PROMPT formal project-name / reviewer-address recipe; restated unlabelled to the deciding turn at `:2374-2380`; literal phrase triggers `:4750-4752` + `:4267-4297`; record-side classifiers `graph/read.rs:2507-2517`, `:2603-2614`; I7 passes but does not park an I5 family compiler | Hogwarts_v5 v2, cometx03 (cometx04 cleared only after removal), CheerUp_v12 v2, rick01; same-hotkey Pentagon v2/v3 |
| 4 | Clear v14 | `db9b919d-241b-4131-a2cc-f426b4356950` | `f498e812cefe…` | `5HpjMRFt…` | ATH board (composite 0.80902) | v12 — I7 (+ I5 companion) | **Reject** (supersedes 2026-09-08 clear) | `agent.rs:327-354` host `forced_read_only` from phrase classifiers → `Mode::ReadOnly` before any model routing → `model.rs:683-705` request carries no tools/`tool_choice` → `agent.rs:528-536` `tool_calls: Vec::new()`; triggers `intent.rs:1116-1256` `missing_action_value` typo ladder, `:1648-1676` declarative preference; I5: `agent.rs:924-938`, `:1155-1177` update-note exact `subject is value` format | Clear v23 (same `agent.rs:327-354` path), Clear v25, Diana v1/v2, NM v8; contrast Pentagon v1 (cleared) and whoamI (cleared) |
| 5 | unione v27 | `dd0783d1-b592-4de1-a788-ca7646b27f51` | `4e9d718f0964…` | `5CkTYaiQ…` | ATH board (no prior ATH review) | v12 — I5, I7 (each independently fatal) | **Reject** | I5: `memory/facts.rs:14-47` closed value-kind registry with sign contracts, `world.rs:3796-3915` `amount_kind_within` cue/typo ladder, `:3682-3735` lesson inventory, rendered as "Factual index" via `world.rs:2767-2771` → `baseline.rs:9026-9029`; `baseline.rs:3471-3531` balance sign sheet + net-movement output format; `:2008-2031` option-identifier recipe; I7: mutation singleton `baseline.rs:4963-4990` + `:8766-8772`, read-route subset `:5131-5156` + `:8628-8639`, id acceptance gate `:3185-3204`, AgentJob verb table `:8803-8822` | rick01, unione v13/v16/v21/v22/v23 (same hotkey lineage), lets_v602, Arbos4u v24/v25, rick12, Jackie-05, 710-v01, lets_622, recall-v1, lets_v609; contrast whoamI |

Executed counts: 5 opened, 5 resolved reject, 5 confirmed on re-read (`held_score_count` 3 each, all SHA and score-count guards passed at first attempt). No rows left for a later fire.

## Notes

- **Superseded clears.** lets_623 v1's 2026-09-11 clear inspected only
  `financial.py` and never read the I5 grounds above; Clear v14's 2026-09-08
  clear recorded no file:line and relied on tool-bearing traces that cannot
  observe the turns the host emptied. A prior clear that never reached the
  construct is not a skip.
- **Charged vs credited.** For every row the credited invariants (e.g. I1
  genuine inference, I4 request-independent folding or untrusted evaluator, I6
  receipt-backed tool_calls) are recorded in the fixture reasons and were not
  charged. Model authorship of the final string does not cure I5; keeping the
  catalog elsewhere does not cure a branch that empties it.
- **Remediation paths.** Pentagon v1 (remove the `serve.rs:79` / `:2380`
  recipe, the `:4750-4752` / `:4267-4297` phrase keys, the `read.rs`
  fixture-shaped classifiers, and the `:53-63` family prose) and unione v27
  (drop sign-contract labels and classifiers from anything shown to the model,
  strip the sign sheet, keep every supplied memory tool in the deciding
  catalog, forward model-authored opaque ids) were given explicit good-faith
  fix lists in their rulings.
- **Benchmark weaknesses are separate from enforcement.** Weaknesses in the
  benchmark itself observed during the sweep belong in benchmark review, not in
  these miner-visible reasons.
