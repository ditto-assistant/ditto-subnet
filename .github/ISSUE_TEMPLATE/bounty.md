---
name: SN118 Maintenance Bounty
about: Propose or track maintenance and engineering tasks for SN118
title: "[BOUNTY] <Short descriptive task title>"
labels: ["bounty", "status:open"]
assignees: []
---

## Bounty Specification

- **Bounty ID**: `SN118-BOUNTY-<YYYY>-<NNN>`
- **Spec Revision**: `1.0.0`
- **Reviewer / Technical Owner**: `@<maintainer_username>`
- **Reward Status**: Proposed / Unfunded (Pending governance activation under #2044)
- **Reward Cap / Amount**: `<Amount> TAO (Proposed)`
- **Funding Source**: `SN118 5% Maintenance Treasury (Shadow Rehearsal)`
- **Claim Expiry Window**: `7 days from reservation`
- **Component**: `component:<miner|validator|platform|backroom|dittobench|infra|security>`

## Scope & Deliverables

- [ ] Deliverable 1: <Detailed description of component change>
- [ ] Deliverable 2: <Detailed description of interface or data contract change>
- [ ] Unit and integration test coverage following repository standards in AGENTS.md

## Dependencies & Conflicts

- **Prerequisite Issues / PRs**: None
- **Known Conflicting Work**: None

## Acceptance Criteria & Evidence Verification

- [ ] Reproduction log / test execution output demonstrating fix or feature.
- [ ] DittoBench benchmark score delta or accuracy logs (where component impacts inference/scoring).
- [ ] Component-specific lint and static analysis passing locally.
- [ ] Staging / canary deployment logs where runtime deployment is required.

## Policy & Payment Decoupling Notice

All work is governed by [docs/CONTRIBUTING_BOUNTIES.md](docs/CONTRIBUTING_BOUNTIES.md) and Parent Epic #2054.

Pull request merge does NOT authorize payment. Merge, deployment/verification, deliverable acceptance, and treasury payout are four distinct states. Payouts require formal maintainer acceptance and chain receipt reconciliation against the treasury ledger.

## Claiming & Workflow

1. Review [docs/CONTRIBUTING_BOUNTIES.md](docs/CONTRIBUTING_BOUNTIES.md).
2. Claim work publicly by commenting on this issue with your hotkey attestation (`ditto-bounty-claim:v1`). No private DMs.
3. Open a Pull Request referencing `Fixes #<this_issue_number>`.
