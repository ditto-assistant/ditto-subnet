# 🏆 Ditto Subnet (SN118) Bounty Contributor Guide

Welcome to the SN118 Bounty Board! This guide provides a canonical workflow for finding, claiming, submitting, and getting paid for maintenance work.

## 📌 1. Finding & Claiming Bounties
We use a public GitHub Project board to track all bounties. You do **not** need a private DM to claim work.

1. **Check the Board**: Visit our [Bounty Board](#) (Link to GitHub Project) to see issues in the `Open` column.
2. **Claiming**: To claim an open bounty, leave a comment on the issue stating: `"I would like to claim this bounty."`
3. **Claim Signing**: A core reviewer will assign the issue to you and move it to the `Claimed` column. 

*Note: Stale claims (no activity for 7 days) may be unassigned and reopened.*

## 💻 2. Execution & Pull-Request Linking
1. Fork the repository and create your feature branch.
2. Ensure your work strictly meets the **Acceptance Criteria** defined in the bounty issue.
3. Open a Pull Request (PR) and link it to the issue by adding `Closes #<Issue_Number>` in the PR description.
4. The issue will automatically move to the `In Review` column.

## 🔬 3. Review & Deployment Evidence
1. The designated **Reviewer** (listed in the bounty issue) will review your PR.
2. You must provide necessary **deployment evidence** (e.g., CI/CD logs, screenshots of passing tests, or live deployment links) in the PR comments.
3. Once approved and merged, the issue moves to the `Accepted` column.

## 💸 4. Payout
1. After the PR is merged, the team will initiate the payout process.
2. **Do not treat labels (like `Accepted`) as automatic payment authorization.** Payments are processed manually based on the reward specified in the issue.
3. Once the transaction is confirmed on-chain, the issue will be labeled `status: paid` and moved to the `Paid` column. If a bounty is closed without payment (e.g., criteria not met), it will be labeled `status: closed-without-payment`.

## ⚖️ 5. Disputes
If you believe a PR was unfairly rejected or payment is delayed, please tag the `@Reviewer` in the issue and open a thread in the `#sn118-bounties` Discord channel.

## 🏷️ Label Glossary
Our automation mirrors the state of the bounty using the following labels:
- `status: open`: Ready to be claimed.
- `status: claimed`: Assigned to a contributor.
- `status: in-review`: PR submitted.
- `status: accepted`: PR merged, awaiting payout.
- `status: paid`: Bounty has been paid out.
- `status: blocked`: Blocked by external dependencies.
- `status: closed-without-payment`: Cancelled or failed to meet criteria.
