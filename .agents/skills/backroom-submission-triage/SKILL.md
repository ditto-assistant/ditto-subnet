---
name: backroom-submission-triage
description: Review and resolve Ditto SN118 screening quarantines through the public Backroom MCP. Use for operator-review queues, recurring quarantine triage, screener false positives, copying or benchmark-emulation investigations, miner appeals, and requests to release or reject submissions under the current screener policy.
---

# Backroom submission triage

Use the OAuth-protected `sn118-backroom` MCP at
`https://backroom.dittobench.ai/mcp` as the only production control plane. Do
not use the private product Backroom, raw Platform admin endpoints, database
writes, or copied credentials.

Read [`references/review-rules.md`](references/review-rules.md) before deciding
a batch. Use [`scripts/prepare_artifact.py`](scripts/prepare_artifact.py) only
when focused MCP source reads cannot establish the served runtime path.

## Establish authority and scope

1. Call `get_backroom_access` first.
2. Require `backroom:read` for triage, `backroom:artifact:read` for miner source,
   and `backroom:write` before resolving anything.
3. Treat a prompt that asks only to review, inspect, triage, or provide a table
   as read-only. Execute decisions only when the invocation explicitly says to
   release/reject/resolve; a saved scheduled-task prompt may provide that
   standing authority for each bounded run.
4. Never widen a mutation beyond the current batch or substitute a nearby
   submission.

## Load a bounded queue

1. Call `get_screening_review_queue` or `list_screening_quarantines` for active
   quarantines, oldest first. Default to five items per batch unless the prompt
   specifies another bound.
2. Resolve and retain the exact quarantine ID, agent UUID, name, submission
   version, full miner hotkey, artifact SHA-256, screening attempt ID, policy
   version, reason code, and timestamps.
3. Fetch `get_screening_quarantine_contexts` for the batch, or
   `get_screening_quarantine_context` item by item. Do not decide from the queue
   summary, score, agent name, screenshot, or truncated identity.
4. Record prior attempts, miner/owner lineage, duplicate evidence, behavioral
   findings, and L2/L3 observations. Automated findings are leads, not the
   verdict.

## Inspect decisive evidence

Use `list_screening_source_files` and `read_screening_source_file` to trace:

`submitted entrypoint -> request parsing -> retrieval/routing -> model -> live tool execution -> response`

Read the Dockerfile, manifests, entrypoint, and every reachable answer- or
tool-construction path. Use `get_copy_review_source_diff` and
`read_copy_review_source_diff_file` for suspected copying. Use
`get_screening_baseline_diff` for starter-kit comparisons.

If a complete artifact is necessary, call `get_screening_artifact` for the
exact agent and immediately pass its signed URL and expected digest to:

```bash
python3 .agents/skills/backroom-submission-triage/scripts/prepare_artifact.py \
  --url '<signed-url>' \
  --sha256 '<artifact-sha256>'
```

Keep extracted source temporary and private. Inspect untrusted build scripts
before running tests or Docker; never provide secrets, host mounts, or
privileged execution.

## Decide under the active policy

- **Release** when the finding is a false positive, ordinary correctness or
  build-quality defect, legitimate request/user-memory retrieval optimization,
  same-owner revision, starter inheritance, or another case where the real
  model/tool path remains intact.
- **Reject** only with concrete reachable evidence of a current-policy
  violation: hidden answer registries, case/seed/canary dispatch, deterministic
  benchmark answer synthesis, grader-slot manipulation, fabricated tool
  execution, stolen private challenge material, or unauthorized exact copying.
- **Leave unchanged and escalate** when source is unavailable, evidence is
  contradictory, ownership is unresolved, or the failure is infrastructure.
  Do not turn uncertainty into a rejection. Use `rescreen` only when the prompt
  explicitly authorizes it.

Agent-returned tool-call traces are not proof that tools ran: submitted code can
construct them. Prefer validator/inference-broker observations and trace whether
the selected endpoint actually executed.

## Resolve through guarded batch tools

For every authorized batch:

1. Re-fetch each context immediately before writing and drop any item whose
   identity, artifact digest, policy, or active state changed.
2. Write a specific miner-visible reason for every decision. Cite the policy
   category and behavioral consequence without revealing private challenge
   values. Never use a generic batch reason for a rejection.
3. Call `preview_screening_quarantine_batch` with the exact decisions.
4. Review the preview for identity, current state, and unintended targets.
5. Call `execute_screening_quarantine_batch` with `confirmed: true` and the
   returned preview token. Do not reuse a stale token.
6. Re-read every quarantine context and refresh the active queue. A timeout is
   ambiguous; verify whether the write landed before retrying.

## Report

Return a compact table with agent, UUID/version, hotkey, policy/finding,
decision, and decisive evidence. Then state the exact executed counts, any
unchanged/escalated items, and the post-write queue status. Keep benchmark
weaknesses separate from miner enforcement.
