---
name: backroom-board-review
description: Review high-scoring SN118 leaderboard agents through Backroom ATH, citing local and live precedents before clear or reject. Use for board review, high-score fire, ATH holds, miner appeals of scored agents, family-compiler or zero-token suspicion, scheduled review loops, and any request to search precedents. Use when the user runs /backroom-board-review.
---

# Backroom board review

Use the OAuth-protected `sn118-backroom` MCP at
`https://backroom.dittobench.ai/mcp` as the only production control plane.

This skill reviews **scored or live** high-rank agents. Screening quarantines
belong to `$backroom-submission-triage`.

Read [references/review-bar.md](references/review-bar.md) before deciding.
Search precedents before opening or resolving a hold.

## Authority

1. Call `get_backroom_access` first.
2. Require `backroom:read` to inspect, `backroom:artifact:read` for source,
   and `backroom:write` before `open_ath_review` or `resolve_ath_review`.
3. Inspect-only unless the invocation explicitly says to hold, clear, reject,
   or autonomously manage the queue. A saved scheduled-task prompt may supply
   that standing authority for one bounded fire.
4. Never substitute a nearby UUID, SHA, or same-hotkey ancestor.

## Courts cite precedents

Search **both** reporters, then apply the same holding to the same pattern.

Live production holdings (authoritative when they exist):

```
search_ath_precedents  query="<pattern>"  [resolution=clear|reject]
```

Local canned holdings and procedure:

```bash
python3 .agents/skills/backroom-board-review/scripts/search-precedents.py "<pattern>"
python3 .agents/skills/backroom-board-review/scripts/search-precedents.py --list
python3 .agents/skills/backroom-board-review/scripts/search-precedents.py --resolution reject --tag family-compiler
```

Cite the matching file or live `agent_id` in the miner-visible reason. If live
and local disagree, the live holding wins for that exact pattern; say so.

## Load a bounded board

1. Call `get_leaderboard` (current bench) and `get_screening_review_queue`.
2. Resolve every target to exact `agent_id`, name, version, full hotkey,
   artifact SHA-256, `agent_status`, and score count. Screenshots, rank, and
   truncated keys are leads.
3. Skip `banned` rows. Leave `evaluating` / `waiting_validator` rows for the
   next fire — `open_ath_review` 409s until the agent is `scored` or `live`.
4. Ban is per UUID. Same SHA or same hotkey is a different row (see
   [references/precedents/identity-uuid-not-sha.md](references/precedents/identity-uuid-not-sha.md)).
5. Re-apply the same bar to every new high-score row. Fairness is identical
   criteria, not identical outcome.

## Inspect the served path

Download when focused MCP reads cannot settle the two limbs. Use
`get_screening_artifact` and:

```bash
python3 .agents/skills/backroom-submission-triage/scripts/prepare_artifact.py \
  --url '<signed-url>' \
  --sha256 '<artifact-sha256>'
```

Trace `request -> retrieval/routing -> model -> live tool -> graded slot`.
Search terms and class labels live in
[references/techniques.md](references/techniques.md).

## Decide

Fail either limb in the review bar, or fail the production-engine test, and
the row is emulation. Prompt grounding, schema-derived args, and a model that
still authors the graded slot at its own score cost are not violations.

Leave unchanged when source is missing, evidence is mixed, or the agent is
not yet scored. Do not turn uncertainty into a reject.

## Resolve

1. Re-fetch `get_screening_submission` immediately before writing.
2. `open_ath_review` with the exact SHA-256 and score count, then
   `resolve_ath_review` `clear` or `reject`.
3. Write a specific miner-visible reason: pattern, file:line, which limb or
   engine test failed or passed, and the cited precedent. No generic batch
   text. No private challenge values.
4. Re-read the agent. A timeout is ambiguous; verify before retrying.

Do not un-ban a row to restore a bench version. Rollout authority is a
Platform question, not an enforcement undo.

## Report

Table: agent, UUID/version, hotkey, SHA, score, precedent cited, decision,
decisive path. Then executed counts and any rows left for a later fire.
