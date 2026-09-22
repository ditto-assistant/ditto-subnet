# SN118 Bounty Board and Contributor Guide

This document defines the operational workflow, state machine, claiming protocol, and accounting standards for maintenance bounties on Bittensor Subnet 118 (SN118), tracked under parent epic #2054.

## Core Governance and Treasury Status

The SN118 5% Maintenance Treasury (issue #2044) funds verified engineering, infrastructure, screening, and benchmark tasks.

### Activation and Funding Principles
- **Governance Precedence**: The treasury operates in shadow and rehearsal mode until governance approves funding sources, custody, and allocation caps. All rewards listed on active bounty issues remain proposed or unfunded until an operator confirms a revisioned reward and funding allocation.
- **Structural State Decoupling**: Pull request merge does NOT authorize payment. Submission, merge, deployment verification, deliverable acceptance, and payment are distinct operational states.
- **No Private Promises**: Discord discussions or informal GitHub comments do not constitute a payment promise or claim reservation.

## Public Project Views and State Machine

Work is organized across distinct states mirrored by issue and pull request labels:

```text
[status:open] ---> [status:claimed] ---> [status:in-review] ---> [status:accepted] ---> [status:paid]
      |                                        |
      +---> [status:blocked]                   +---> [status:cancelled]
```

### State Labels
- `status:open`: Bounty is scoped, reviewed, and open for contributor claims.
- `status:claimed`: Contributor holds an active, time-bounded reservation (7-day default).
- `status:in-review`: Pull request submitted and actively linked for review.
- `status:accepted`: Deliverable verified and formally accepted by designated reviewers.
- `status:paid`: Payout executed on-chain and reconciled against the treasury ledger.
- `status:blocked`: Progress halted due to upstream dependencies or governance holds.
- `status:cancelled`: Bounty closed without payment, withdrawn, or rejected.

### Public Board Queries
Contributors and operators track tasks using public GitHub queries:

| View | Filter Query |
|---|---|
| Open Work | `is:issue is:open label:bounty -label:status:claimed -label:status:in-review` |
| Claimed Work | `is:issue is:open label:bounty label:status:claimed` |
| In Review | `is:issue is:open label:bounty label:status:in-review` |
| Accepted Work | `is:issue label:bounty label:status:accepted` |
| Paid Work | `is:issue label:bounty label:status:paid` |
| Blocked Work | `is:issue is:open label:bounty label:status:blocked` |
| Closed Without Payment | `is:issue is:closed label:bounty -label:status:paid` |

### Querying Stale and Blocked Claims
- **Stale Claims**: To find reservations without activity exceeding the 7-day TTL, query:
  `is:issue is:open label:bounty label:status:claimed updated:<YYYY-MM-DD`
- **Blocked Claims**: To review work waiting on external resolutions, query:
  `is:issue is:open label:bounty label:status:blocked`
- **Accounting Distinction**: Closed unpaid work (`status:cancelled` / closed as not planned) and settled work (`status:paid` / closed as completed with receipt) remain queryable and distinct in both GitHub and the accounting ledger.

## Claiming and Reservation Protocol

Work must be claimable by any miner or external contributor without private direct messages (DMs).

### 1. Claiming Without Private DMs
All claims must be submitted publicly as a comment on the respective bounty issue. Private communications on Discord, Telegram, or email cannot establish or reserve a claim.

### 2. Hotkey Identity and Claim Contract
To prevent Sybil reservations and impersonation, claims bind to a verifiable Subnet 118 identity using the `ditto-bounty-claim:v1` signing format (issue #2045):

```json
{
  "domain": "ditto-bounty-claim:v1",
  "netuid": 118,
  "repo": "ditto-assistant/ditto-subnet",
  "issue_number": 2047,
  "spec_revision": "1.0.0",
  "claimant_hotkey": "5Gh...",
  "key_kind": "sr25519",
  "nonce": "6a9f...",
  "issued_at": 1758500000,
  "reservation_expiry": 1759104800
}
```

- **Payout Target Binding**: Payout destinations resolve from the claimant hotkey's on-chain coldkey owner (`SubtensorModule.Owner`). GitHub user accounts cannot redirect funds to third-party addresses.
- **Reservation Window**: Default reservation window is 7 days. If no linked pull request with demonstrable progress is submitted within 7 days, the reservation expires and the issue returns to `status:open`.
- **Renewals and Handoffs**: Contributors may request an extension by posting an update with intermediate artifacts before the 7-day expiration. Handoffs between contributors must be recorded publicly on the issue with mutual signed attestations.
- **Revocation**: Reservation revocation requires an audited reason posted publicly by the technical owner.

## Contributor Execution and Local Validation

Contributors must consult `AGENTS.md` for general repository standards, architecture boundaries, and execution rules. Do not rely on generic test commands across the monorepo.

### Component Validation Matrix
Run validation commands specific to the modified subsystem:

| Component | Scope | Verification Command |
|---|---|---|
| `component:miner` | Miner harness & starter kits | `cargo test`<br>`cargo run -- evaluate`<br>`cargo run -- practice --n 20` |
| `component:validator` | Validator worker & stack | `uv run pytest ditto/tests/validator/`<br>`uv run pytest ditto/tests/test_validator_compose.py` |
| `component:platform` | API server & database | `uv run pytest apps/platform/ditto/tests/`<br>`npm --prefix apps/platform/dashboard test` |
| `component:backroom` | Operations console & MCP | `uv run pytest apps/backroom/tests/` |
| `component:dittobench` | Go benchmark scorer | `cd services/dittobench-api && go test ./...` |
| `component:infra` | Docker & CI/CD | `python3 .github/scripts/check_workflow_security.py`<br>`uv run pytest ditto/tests/test_workflow_security.py` |
| Root / Monorepo | Core Python contracts | `uv sync --locked --group dev`<br>`make lint`<br>`make typecheck`<br>`make test` |

### Conventions
- **Commit Titles**: Must follow conventional commit types (`feat`, `fix`, `docs`, `chore`, `perf`, `refactor`). Test-only changes must use `chore(tests):` (do not use `test:` prefix).
- **Draft Pull Requests**: Contributors should open a Draft PR early to stake progress and record intermediate commits.
- **Issue Linking**: PR descriptions must include `Fixes #<issue_number>` or `Resolves #<issue_number>` to establish state traceability.

## Deliverable Evidence and Verification

Every pull request for a bounty must supply objective evidence in the PR description:
1. **Benchmark Results**: For changes touching scoring, evaluation, or miner performance, attach before-and-after DittoBench run logs.
2. **Test Suite Execution**: Provide complete local test pass logs for the targeted component.
3. **Deployment Artifacts**: For changes requiring infrastructure or runtime updates, supply staging or canary run verification outputs.

## Review, Deployment, Acceptance, and Payment

Transitions through the final stages require meeting four independent gates:

### Gate 1: Code Review and Integration
- Designated reviewer conducts technical review against acceptance criteria.
- PR is merged into `main`. The issue transitions to code-complete status, but remains unpaid.

### Gate 2: Deployment and Runtime Verification
- Changes are deployed to staging, canary workers, or screener fleets.
- Runtime metrics and stability verify absence of regressions.

### Gate 3: Formal Deliverable Acceptance
- Designated reviewer validates complete fulfillment of all acceptance criteria.
- Issue transitions to `status:accepted`.
- Bounties exceeding large-payout thresholds require multi-reviewer sign-off as defined in issue #2046.

### Gate 4: On-Chain Settlement and Public Receipt
- Payout is initiated from the treasury coldkey via `Balances.transfer_keep_alive`.
- Transaction reaches finality and produces an `ExtrinsicSuccess` receipt.
- The transaction hash, block number, issue reference, commit SHA, and recipient address are recorded in the public append-only audit ledger.
- Issue transitions to `status:paid`.

## Disputes and Appeals

- If a submission is rejected or closed without payment, the contributor may appeal directly on the issue thread by citing specific deliverables and acceptance logs.
- Appeals are reviewed by a secondary subnet maintainer.
- Partial awards may be allocated if substantial components were accepted or if work was divided collaboratively.

## Links and External Ingress

- **DittoBench**: Links to the active bounty board from the platform dashboard and benchmark documentation.
- **Discord**: The `#bounties` channel in the official Ditto Discord points to the GitHub Project board. Discord is restricted to discussion and coordination; all claims, reviews, and decisions must occur publicly on GitHub.
