---
name: backroom-review
description: Review Ditto SN118 miner submissions through Backroom — screening quarantines and scored high-rank ATH holds. Use for operator-review queues, recurring quarantine triage, screener false positives, copying or benchmark-emulation investigations, board review, high-score fire, ATH holds, miner appeals, family-compiler or zero-token suspicion, scheduled review loops, precedent search, and requests to release, reject, clear, or hold an agent. Use when the user runs /backroom-review, /backroom-board-review, or /backroom-submission-triage.
---

# Backroom review

Use the OAuth-protected `sn118-backroom` MCP at
`https://backroom.dittobench.ai/mcp` as the only production control plane. Do
not use the private product Backroom, raw Platform admin endpoints, database
writes, or copied credentials.

Two courts, one control plane:

- **Quarantine** — active screening holds. Read
  [references/review-rules.md](references/review-rules.md).
- **ATH board** — scored or live high-rank agents. Read
  [references/review-bar.md](references/review-bar.md) and search precedents
  before opening or resolving a hold.

## Authority

1. Call `get_backroom_access` first.
2. Require `backroom:read` to inspect, `backroom:artifact:read` for source,
   and `backroom:write` before any mutation.
3. Inspect-only unless the invocation explicitly says to release, reject,
   resolve, hold, clear, or autonomously manage the queue. A saved
   scheduled-task prompt may supply that standing authority for one bounded
   fire.
4. Never substitute a nearby UUID, SHA, or same-hotkey ancestor, and never
   widen a mutation beyond the current batch.

## Choose the court

Resolve the target to exact `agent_id`, name, version, full hotkey, artifact
SHA-256, and `agent_status`. Screenshots, rank, and truncated keys are leads.

- Active quarantine / review queue / `list_screening_quarantines` → quarantine.
- Leaderboard / ATH / scored or live high-rank / precedent search → board.
- Miner appeal: follow current status. A scored agent is a board case even
  if it was once quarantined.
- Skip `banned` rows. Park `evaluating` / `waiting_validator` for the next
  fire — `open_ath_review` 409s until the agent is `scored` or `live`.
- Ban is per UUID. Same SHA or same hotkey is a different row (see
  [references/precedents/identity-uuid-not-sha.md](references/precedents/identity-uuid-not-sha.md)).

## Inspect the served path

Use `list_screening_source_files`, `read_screening_source_file`, and
`search_screening_source` to trace:

`submitted entrypoint -> request parsing -> retrieval/routing -> model -> live tool -> graded slot`

Read the Dockerfile, manifests, entrypoint, and every reachable answer- or
tool-construction path. Use `get_copy_review_source_diff` and
`read_copy_review_source_diff_file` for suspected copying or same-owner
resubmits. Use `get_screening_baseline_diff` for starter-kit comparisons.
Search terms and class labels live in
[references/techniques.md](references/techniques.md).

Download when focused MCP reads cannot settle the path. Call
`get_screening_artifact` for the exact agent and immediately pass its signed
URL and expected digest to:

```bash
python3 .agents/skills/backroom-review/scripts/prepare_artifact.py \
  --url '<signed-url>' \
  --sha256 '<artifact-sha256>'
```

Keep extracted source temporary and private. Inspect untrusted build scripts
before running tests or Docker; never provide secrets, host mounts, or
privileged execution. Smoke `--help` / `/health` only unless broader
execution is authorized.

Agent-returned tool-call traces are not proof that tools ran: submitted code
can construct them. Prefer validator/inference-broker observations and
trace whether the selected endpoint actually executed.

## Quarantine

1. Call `get_screening_review_queue` or `list_screening_quarantines` for
   active quarantines, oldest first. Default to five items unless the prompt
   specifies another bound.
2. Retain the exact quarantine ID, agent UUID, name, version, full hotkey,
   artifact SHA-256, screening attempt ID, policy version, reason code, and
   timestamps.
3. Fetch `get_screening_quarantine_contexts` for the batch, or
   `get_screening_quarantine_context` item by item. Record prior attempts,
   miner/owner lineage, duplicate evidence, and L2/L3 observations.
   Automated findings are leads, not the verdict.
4. Decide under [references/review-rules.md](references/review-rules.md):
   - **Release** when the real model/tool path remains intact.
   - **Reject** only with concrete reachable current-policy evidence.
   - **Leave unchanged and escalate** when source is missing, evidence is
     mixed, ownership is unresolved, or the failure is infrastructure.
   - `rescreen` only when the prompt explicitly authorizes it.
5. Re-fetch each context immediately before writing and drop any item whose
   identity, digest, policy, or active state changed.
6. Write a specific miner-visible reason. Cite the policy category and
   behavioral consequence. No generic batch reject text. No private
   challenge values.
7. `preview_screening_quarantine_batch`, review identity and current state,
   then `execute_screening_quarantine_batch` with `confirmed: true` and the
   returned preview token. Do not reuse a stale token.
8. Re-read every quarantine context and refresh the queue. A timeout is
   ambiguous; verify whether the write landed before retrying.

## ATH board

Search **both** reporters, then apply the same holding to the same pattern.

Live production holdings (authoritative when they exist):

```
search_ath_precedents  query="<pattern>"  [resolution=clear|reject]
```

Local canned holdings:

```bash
python3 .agents/skills/backroom-review/scripts/search-precedents.py "<pattern>"
python3 .agents/skills/backroom-review/scripts/search-precedents.py --list
python3 .agents/skills/backroom-review/scripts/search-precedents.py --resolution reject --tag family-compiler
```

Cite the matching file or live `agent_id` in the miner-visible reason. If
live and local disagree, the live holding wins for that exact pattern; say
so.

1. Call `get_leaderboard` (current bench) and `get_screening_review_queue`.
2. Resolve every target. Re-apply the same bar to every new high-score row.
   Fairness is identical criteria, not identical outcome.
3. Fail either limb in the review bar, or fail the production-engine test,
   and the row is emulation. Prompt grounding, schema-derived args, and a
   model that still authors the graded slot at its own score cost are not
   violations.
4. Leave unchanged when source is missing, evidence is mixed, or the agent
   is not yet scored. Do not turn uncertainty into a reject.
5. Re-fetch `get_screening_submission` immediately before writing.
6. `open_ath_review` with the exact SHA-256 and score count, then
   `resolve_ath_review` `clear` or `reject`.
7. Write a specific miner-visible reason: pattern, file:line, which limb or
   engine test failed or passed, and the cited precedent.
8. Re-read the agent. A timeout is ambiguous; verify before retrying.

Do not un-ban a row to restore a bench version. Rollout authority is a
Platform question, not an enforcement undo.

## Report

Table: agent, UUID/version, hotkey, SHA, court, policy/finding or
precedent, decision, decisive path. Then executed counts and any rows left
for a later fire. Keep benchmark weaknesses separate from miner
enforcement.
