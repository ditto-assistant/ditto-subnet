# SN118 Bounty Board & Contributor Guide

> **Status:** Living document. The GitHub issue tracker is the **single source of truth** for all SN118 bounties.
> This guide explains how to discover, claim, complete, and get paid for bounty work — and how maintainers post well-specified bounties.

---

## 1. How the board works

SN118's bounty program is **GitHub-backed**: every bounty is an issue in this repository, labeled so it can be filtered and discovered without any external service.

To see all open bounties:

```
https://github.com/ditto-assistant/ditto-subnet/issues?q=is%3Aopen+label%3Abounty
```

To see the easiest entry points:

```
https://github.com/ditto-assistant/ditto-subnet/issues?q=is%3Aopen+label%3Abounty+label%3A%22good+first+issue%22
```

There is **no separate board application, no signup, and no off-platform coordination**. If it isn't an issue here, it isn't a bounty.

---

## 2. Label taxonomy

| Label | Meaning | Contributor action |
|---|---|---|
| `bounty` | Issue carries a reward | Eligible to claim |
| `good first issue` | Small, well-scoped, low context needed | Ideal first contribution |
| `help wanted` | Maintainers actively want outside help | Claim freely |
| `documentation` | Docs/spec only, no runtime change | Great for spec writers |
| `enhancement` | New feature or behavior | Read acceptance criteria carefully |
| `epic` | Umbrella tracking issue | Do **not** claim directly; claim its children |
| `bug` | Defect to fix | Include a repro in your PR |

> **Rule:** an `epic` is a container, not a task. Always claim the concrete child issue, never the epic itself.

---

## 3. Reward tiers

Rewards are stated **in the issue title** in the form `[BOUNTY $X]`. Tiers below describe expected scope and review depth — actual reward is always the number in the issue.

| Tier | Typical reward | Scope | Review depth |
|---|---|---|---|
| **Trivial** | $10–$50 | Single file, docs, one-liner fix | 1 reviewer, fast-track |
| **Standard** | $50–$150 | Self-contained artifact (skill, template, hook, agent) | 1 reviewer, functional test expected |
| **Advanced** | $150–$300 | Multi-file feature, integration, or workflow | 2 reviewers, tests required |
| **Epic** | $300+ | Multi-issue initiative | Tracked across child issues |

**Payout is on merge.** Partial credit is not offered; if scope must be reduced, maintainers will split the issue so each part is independently payable.

---

## 4. Contribution workflow

### 4.1 Discover
Filter by `bounty` (see §1). Prefer `good first issue` when starting out.

### 4.2 Claim
Comment on the issue with a short plan:

```
I'd like to work on this. Plan:
- <step 1>
- <step 2>
ETA: <n> days.
```

Maintainers may assign the issue to you. **One active claim per contributor per tier** unless stated otherwise, to keep throughput fair.

### 4.3 Branch
```bash
git clone https://github.com/<you>/ditto-subnet.git
cd ditto-subnet
git checkout -b fix/bounty-issue-<ISSUE_NUMBER>
```

### 4.4 Implement
- Match the **artifact type named in the issue title** (e.g. `SKILL`, `HOOK`, `TEMPLATE`, `AGENT`, `WORKFLOW`, `docs`). Build exactly that primitive.
- Keep the change **self-contained** where the issue allows it.
- Add tests when the deliverable is executable.

### 4.5 Open the PR
```bash
git add -A
git commit -m "fix: resolve bounty issue #<ISSUE_NUMBER>"
git push origin fix/bounty-issue-<ISSUE_NUMBER>
```

Open a PR against `main` and reference the issue:

```
Resolves #<ISSUE_NUMBER>
```

### 4.6 Review → merge → payout
A maintainer reviews against the issue's **acceptance criteria**. On merge, the bounty is settled. Payout records feed the public accounting tracked in **#2046**.

---

## 5. Issue template (for maintainers)

Copy this when posting a new bounty:

```markdown
---
title: "[BOUNTY $<amount>] <TYPE>: <one-line summary>"
labels: bounty, <good first issue | help wanted | documentation | enhancement>
---

## Context
<Why this matters. 2–4 sentences.>

## Deliverable
<Exact artifact: file path(s), format, language/runtime.>

## Acceptance criteria
- [ ] <criterion 1>
- [ ] <criterion 2>
- [ ] <criterion 3>

## Definition of done
- [ ] Merged PR resolving this issue
- [ ] Tests included (if executable)

## Reward
$<amount> USDC, paid on merge.

## Out of scope
<Explicit non-goals, to prevent scope creep.>
```

A bounty without **acceptance criteria** is not claimable — maintainers must specify them before applying the `bounty` label.

---

## 6. PR checklist (for contributors)

Before requesting review, confirm:

- [ ] PR body contains `Resolves #<issue>`.
- [ ] Branch is named `fix/bounty-issue-<issue>`.
- [ ] Deliverable matches the artifact type in the issue title.
- [ ] All acceptance criteria are addressed (tick them in the PR body).
- [ ] Tests added/updated for executable artifacts.
- [ ] No unrelated changes; diff is scoped to the issue.
- [ ] Documentation updated if behavior changed.

A PR missing any of the above will be sent back with a change request rather than merged.

---

## 7. Review & payout policy

| Stage | Target SLA |
|---|---|
| First maintainer response | 3 business days |
| Full review after criteria met | 5 business days |
| Payout after merge | at settlement cadence (see #2046) |

**Disputes.** If a PR meets the stated acceptance criteria but is not merged, open a comment on the issue tagging a maintainer. Dispute resolution and payout proof are specified in **#2046**; treasury funding for the program is specified in **#2044**.

**Rejected work.** If a submission does not meet criteria, the reviewer must state *which criterion* failed so the contributor can revise. Silent rejection is not permitted.

---

## 8. Contributor guide

### Local setup
```bash
git clone https://github.com/ditto-assistant/ditto-subnet.git
cd ditto-subnet
# follow the repo README for language-specific install steps
```

### Code style
- Follow the conventions already present in the directory you touch.
- Prefer small, focused commits with imperative messages (`fix:`, `docs:`, `feat:`).
- No new dependencies unless the issue explicitly requires them.

### Testing expectations
- Executable deliverables ship with tests runnable via a single documented command.
- Documentation deliverables require no tests, but must be accurate and self-contained.

### Where to ask questions
- Open a comment on the bounty issue itself (preferred — keeps context in one place).
- For general questions, open a `question`-labeled issue.

### First contribution path
1. Filter `bounty` + `good first issue`.
2. Claim one issue with a short plan.
3. Open a scoped PR referencing the issue.
4. Respond to review feedback within one cycle.

---

## 9. Relationship to other SN118 work

| Issue | Relationship |
|---|---|
| **#2044** | Treasury & governance spec — funds the bounty program. |
| **#2046** | Bounty acceptance, payout proof, disputes, public accounting. |
| **#2047** | *This document* — the board and contributor guide. |
| **#2054** | Epic: 5% maintenance treasury and miner bounty operations. |

This guide is intentionally the **discovery layer**; #2046 is the **settlement layer**; #2044 is the **funding layer**. Together they form a complete, auditable bounty program.

---

*Documentation only. No runtime, subnet, or smart-contract behavior is changed by this file.*
