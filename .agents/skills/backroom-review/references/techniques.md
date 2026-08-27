# Source inspection techniques

## Resolve identity first

- Prefer a full agent UUID from the user, leaderboard, or queue.
- `get_screening_submission` + `get_miner_owner_footprint` for lineage.
- `get_ath_review` when any hold exists; a pending row whose `agent_status`
  is not `ath_pending_review` is stranded (409 on resolve).
- Do not treat a hybrid v10 board row as the v11 artifact you just banned.

## Search the served path, do not read blind

Use `search_screening_source` (or `rg` on an extracted tree) before paging
400-line windows. Start with:

```
RunResponse|prompt_tokens:\s*0|VERIFIED RESULT|glossary_block
established_for_prompt|settled_without_operation|author_reconciled_value
EXACT_VALUE_PROMPT|system_prompt = compact|try_solve|fn family_of
v10_open_program|phrase|Role::PHRASES|for attempt in
REPLY WITH EXACTLY|WJFAST|reject.until
prepared_state_worksheet|_reply_needs_review|_needs_correction|_fallback
requested_answer_components|decisive_worksheet_excerpt|Reply exactly
FINAL ANSWER TARGET|calculated_money_answer|prepare_linked_balance
LINKED_RUNNING_QUANTITY|LINKED_SCOPED|_NET_CHANGE_QUERY_RE
WORKED OUT FROM THIS USER|these lines win|say it exactly
asks_to_reconcile|the one right call|next_required
required_tool_names|predict_relevant_tools|host_tools
_keep_better|ValueKind|_VALUE_KINDS|EXPRESSION_REQUEST
```

Zero hits on banned identifiers is not a pass. Restacks rename the
compiler (`final_source` → `calculated_money_answer`, `SIGN_AUDIT` →
`RUNNING_QUANTITY`, remainder recipes as worksheets). Grep the new
names, then trace every writer of the served text field.

Then `read_screening_source_file` only at the hit. For same-owner resubmits,
`get_copy_review_source_diff` then `read_copy_review_source_diff_file` shows
what actually changed.

## Download when the path is large

`get_screening_artifact` URLs expire in minutes. Pipe them through
`backroom-review/scripts/prepare_artifact.py` so the digest is
checked and the tree stays temporary. Downloading every high-rank agent in
a full-board pass is faster than six MCP windows per file.

Treat the tree as untrusted: no secrets, no host mounts, no privileged
Docker. Smoke `--help` / `/health` only unless broader execution is
authorized.

## What the patterns look like on the wire

| Pattern | Typical hit | Bar |
|---|---|---|
| Zero-token glossary / verified result | `prompt_tokens: 0`, `VERIFIED RESULT` returned as the slot | Class A, limb (b) |
| Compact / empty notes | `system_prompt = compact.clone()`, `established_for_prompt = ""` | Class D, limb (a) |
| Reject-until-match | `for attempt in 1..=5` until grader-shaped text | Class D, limb (b) |
| Author-reconciled value | `settled_without_operation` overwrites the model | Class D, both limbs |
| Family compiler | closed `Program` / `try_solve` / phrase table | production-engine |
| Character-match ladder | 5× Levenshtein / token corrections onto a bench phrase | production-engine |
| Worksheet fallback overwrite | `*_reply_needs_review` value gate + `result.text =` fallback after the review budget (aceron-b12-v5) | Class D, limb (b) |
| Family worksheet + answer target | `FINAL ANSWER TARGET`, `decisive_worksheet_excerpt`, host-computed remaining/net fields (aceron_b12_v1) | production-engine |
| Renamed host overwrite | `calculated_money_answer` writing `result.result.text` (unione v16) | Class D, limb (b) |
| Renamed remainder coach | `LINKED_RUNNING_QUANTITY_DRAFT_PROMPT` ships as the slot (lets_v602) | production-engine |
| Scored-family decline gate | `Reply exactly: "I don't have that information"` + do-not-attempt on an answerable family | Class A, production-engine |
| Copy-authority grounding | `WORKED OUT FROM THIS USER'S OWN RECORDS` + “these lines win” / “say it exactly” (alexandros-v12-19) | I4 |
| Value-kind / operand sheet | closed `_VALUE_KINDS` registry + `EXPRESSION_REQUEST` sign/unit recipes; model still writes the string (rick01) | I5 |
| One-pinned next tool | `next_required` / `required_tool_names` / trained `host_tools` catalog swap (recall-v1) | I7 |
| Content-based review suppress | `_keep_better` on parseable refusals, or discard later review and serve the earlier draft | I3 |

Agent-returned tool traces are not proof the tool ran. Follow the code that
builds `RunResponse`.

## Procedure that has bitten us

- ATH while `evaluating` fails closed. Park the UUID for the next fire.
- After mass rejects, hybrid authority can drop to the previous bench. Do
  not un-ban to restore it. Expand-cohort or a rollout PR is the fix.
- Older same-hotkey rows reappear on the KOTH board (Athena treadmill).
  They are new reviews, not the ban rolling back.
- Same-owner lexical near-duplicates of a rejected artifact auto-hold.
  Diff them; a real remediation clears (Hogwarts v17).
- Never-reviewed emission recipient or scored ≥0.70 successor of a banned
  row: inspect this SHA even if an ancestor was cleared this hour.
- A prior keep of this UUID is not a skip if the cited path is still served.
- `screening_policy_version=10` plus `deferred-mechanical-admission` is not a
  v10 source review. Inspect the served path anyway.
- Zero hits on `WORKED OUT` / remainder-sheet names is not an I5 pass. Look
  for value-kind registries and operand/sign/unit instruction sheets.
- `cfg(measure)` off / Program solver excluded is a credit. Trace I4/I5/I7 on
  the default binary.
- Do not park as mixed because I4 or I7 passed. Score each invariant.
