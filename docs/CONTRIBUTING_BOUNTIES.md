# SN118 Bounty Board & Contributor Guide

Welcome to the **Bittensor Subnet 118 (SN118)** GitHub-backed Bounty Board. This guide outlines the lifecycle of maintenance, engineering, and security bounties funded by the SN118 5% Maintenance Treasury.

---

## 📌 Lifecycle & State Machine

Every bounty progresses through explicit, machine-mirrored states:

```
[status:open] ──> [status:claimed] ──> [status:in-review] ──> [status:accepted] ──> [status:paid]
      │                                       │
      └───> [status:blocked]                  └───> [status:disputed] ──> [status:cancelled]
```

### State Labels:
- `status:open`: Bounty is funded and available for claiming.
- `status:claimed`: A contributor has signaled active work. Claims expire in 7 days if no PR is opened.
- `status:in-review`: Pull Request is submitted and undergoing automated CI and reviewer checks.
- `status:accepted`: Pull Request has been approved by the designated reviewer.
- `status:paid`: Payout has been settled on-chain or via designated payout rails.
- `status:blocked`: Task is blocked by external dependencies or upstream releases.
- `status:cancelled`: Bounty is withdrawn or duplicate.

---

## 🛠️ How to Claim & Execute Work

### 1. Finding & Claiming a Task
- Browse issues tagged with `bounty` and `status:open`.
- Review the acceptance criteria and required verification artifacts.
- Comment `/claim` on the issue. The automated bot will assign the issue and set `status:claimed`.

### 2. Implementation & Local Validation
- Follow the monorepo conventions defined in `CLAUDE.md`.
- Run local pre-submit checks before opening a PR:
  ```bash
  python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py "<task>"
  pytest tests/
  ```

### 3. Submitting the Pull Request
- Create a PR referencing the bounty issue: `Resolves #<issue_number>`.
- Include the **Evidence & Verification** checklist in the PR description:
  - Benchmark throughput or accuracy logs.
  - Passing automated test suite.
  - Clear payout address (Bittensor SS58 address, Base EVM, or PayPal).

### 4. Review & Merge
- The designated reviewer reviews the code within 3–5 business days.
- Once merged, the issue transitions to `status:accepted` and enters the payout batch.

---

## 💳 Payouts, Accounting & Disputes

### Payout Execution:
- Payouts are executed from the SN118 5% Maintenance Treasury wallet.
- Payout transactions are logged publicly in the treasury ledger with on-chain transaction hashes.

### Expiry Rules:
- If a claim has no active PR or demonstrable progress after **7 days**, the claim is released automatically to allow other contributors to participate.

### Disputes:
- If a contributor believes their submission met all criteria but was rejected unfairly, they can request a secondary review by tagging the subnet governance lead in the issue discussion.
