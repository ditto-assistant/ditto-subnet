# Independent LongMem review prompts

Replace bracketed fields and keep each assignment read-only unless it explicitly
owns implementation.

## Implementation owner

```text
Work only in [WORKTREE/STACK]. For a new repair create a fresh origin/main
worktree; for an active PR import/resume its exact gh stack. Never edit the source
checkout. Reproduce [EXACT FAILURE SHAPE], identify the smallest fail-closed
repair, implement it across every producer and verifier boundary, add
focused/integration/adversarial tests, run affected full, race, vet/type, and
release-plan gates, then publish or update a draft PR. Preserve frozen assets and
public privacy. Report exact head/base and do not merge or deploy.
```

## Semantic and retry reviewer

```text
Review PR [NUMBER] at exact head [SHA] read-only. Trace error classification,
context/cancellation priority, retry ownership, scoring, receipt gates, lifecycle
ordering, and all-zero/all-false terminal behavior. Return APPROVE or BLOCKER with
source anchors and focused commands. Do not edit.
```

## Security and accounting reviewer

```text
Review PR [NUMBER] at exact head [SHA] read-only as an adversarial submitted
harness. Try delayed/background calls, stale capabilities, same-IP reuse,
partial receipts, provider identity drift, spend amplification, forged typed
errors, and log/body leakage. Verify no evidence or cost is fabricated or lost.
Return APPROVE or BLOCKER with a concrete exploit/proof. Do not edit.
```

## Contract and release reviewer

```text
Review PR [NUMBER] at exact head [SHA] read-only. Prove Go producer/replay, shared
Python, and Platform validation agree; positive canonical bytes and frozen assets
remain stable unless explicitly versioned; old/new skew fails closed; affected
components and Platform-before-scorer ordering are correct. Return APPROVE or
BLOCKER with exact gates. Do not edit.
```

## Test and adversarial reviewer

```text
Review PR [NUMBER] at exact head [SHA] read-only. Reproduce the production shape,
enumerate malformed/partial/reversed/racy variants, stress focused tests under
race detection, and verify the integration reaches the next real confirmation
boundary. Return APPROVE or BLOCKER and identify nonblocking coverage gaps.
```

## Release watcher

```text
Follow the authoritative main release containing merge [SHA]. Prove semantic
ancestry, immutable artifacts, Platform deploy, stack promotion, and terminal
workflow status. Auto-follow superseding descendant runs. Do not mutate or canary.
```

## Fleet and canary watchers

```text
Read-only: wait for one managed validator to adopt exact release [SHA/DESCRIPTOR],
remain healthy/available/accepting across multiple heartbeats, serve a
fresh_verified scorer, and show no updater failure/rollback. Before issuing
anything, detect an existing ticket and live daily caps. Observe exactly one
bounded LongMem attempt to terminal signed Platform-verified evidence. Never
duplicate, raise caps, retry, cancel, or alter policy without explicit authority.
```
