# SN118 Scoped Build-Bounty Specification, Alpha Funding, and Governance (#2044)

Status: **Canonical Policy Specification (#2044)**. This document defines what
qualifies as a Ditto SN118 build bounty, what work is excluded, how reward
pools and entry fees are denominated and funded in alpha, how rounds and slots
are sized, how governance duties are separated across operators, what must be
publicly auditable, and how anti-abuse rules are enforced.

Companion documents:
- [Bounty claims, slots, and contributor identity](bounty-claims.md) (#2045)
- [Build bounties: contributor guide](bounty-guide.md)
- [Build bounties: operator runbook](bounty-operations.md) (#2046 / #2047)

---

## 1. Qualification Criteria (What Qualifies as a Build Bounty)

An issue qualifies for publication as a paid SN118 build bounty if and only if
its signed `BountyTerms` (`ditto-bounty-spec:v1`) satisfy all six requirements
before any slot opens:

| Requirement | Field(s) in `BountyTerms` | Rule |
|---|---|---|
| **Bounded scope** | `title`, `scope` | Names the exact module(s), package(s), or documentation files in scope and states what is out of scope. |
| **Explicit public interfaces** | `public_interfaces` | Lists the exact CLI flags, HTTP endpoints, Python classes/functions, database tables, or wire schemas that the change adds or modifies. |
| **Required tests & benchmarks** | `acceptance_evidence` | Names the deterministic test command(s) (`uv run pytest ...`) and/or benchmark thresholds that must pass without manual intervention. |
| **Explicit dependencies** | `dependencies` | Lists prerequisite issues as `owner/repo#issue` (empty list `[]` if standalone). |
| **Objective acceptance evidence** | `acceptance_evidence` | Verifiable artifacts (test output, schema migration checks, reproducible CLI transcript) that leave no ambiguity for review or appeal. |
| **Assigned reviewer** | `reviewer_login`, `reviewer_actor` | Names both the public GitHub login (`reviewer_login`) and the internal operator identity (`reviewer_actor`) responsible for reviewing submissions within the review SLA. |

---

## 2. Disqualified Work (What Does Not Qualify)

The following categories of work MUST NOT be published as external build
bounties and are refused at publication review:

1. **Open-ended research or exploratory prototypes** without deterministic
   pass/fail acceptance criteria.
2. **Security-critical signing, hot-wallet, or key-custody paths**, including
   live treasury transfer signing, validator weight-signing keys, and secret
   material handling.
3. **Production authority or access-control changes**, including Backroom
   authentication bypasses, database privilege escalations, or live funding
   activation switches.
4. **Subjective polish or cosmetic restyling** where acceptance depends on
   taste rather than objective tests and interface contracts.

---

## 3. Alpha Denomination, Funding Source, and Fee Custody

### 3.1 Denomination
- All bounty reward pools (`reward_pool_alpha_rao`) and slot entry fees
  (`entry_fee_alpha_rao`) are denominated in **SN118 alpha rao**, where
  `1 alpha = 1,000,000,000 rao (10^9 rao)`.
- Amounts are positive integers bounded by `2^53 - 1` (`9,007,199,254,740,991`
  rao) so every JSON parser and SQL `BIGINT` column represents them without
  precision loss.

### 3.2 Funding Source & Pre-Funding Approvals
- **Rehearsal mode (`funding_mode = "rehearsal"`)**: All entry fees, refunds,
  and awards are simulated via `SimulatedFeeVerifier` and
  `SimulatedTransferExecutor`. No on-chain alpha moves.
- **Live mode (`funding_mode = "live"`)**: Reward pools are sourced from the
  designated SN118 maintenance/bounty treasury account authorized under
  `maintenance-treasury.md`. Before any live bounty is published or paid:
  1. A payout policy (`set_bounty_payout_policy`) must be configured defining
     `single_approver_max_alpha_rao` and `dual_approver_max_alpha_rao`.
  2. Awards and refunds up to `single_approver_max_alpha_rao` require **1**
     non-conflicted treasury operator approval; amounts up to
     `dual_approver_max_alpha_rao` require **2** distinct non-conflicted
     operator approvals; amounts above `dual_approver_max_alpha_rao` are
     blocked (`over_policy_limit`).

### 3.3 Entry Fee Disposition & Forfeited Fee Custody
- Entry fees are paid to the revision's dedicated `fee_escrow_coldkey` /
  `fee_escrow_hotkey` pair (`fee_escrow_coldkey != fee_escrow_hotkey`).
- **Full refund (`fee_refund`)**:
  - When a contributor's submission is **accepted** (`reason = "accepted"`).
  - When a slot is **cancelled by operators** due to team scope change
    (`reason = "cancelled"`) or platform/system fault (`reason = "system_fault"`).
  - When a payment cannot win its slot (`hold_expired`, `slot_lost`,
    `identity_changed`, `duplicate_payment`).
- **Retained / Forfeited (`fee_retained`)**:
  - When a slot is **rejected**, **expired** (missed delivery/rework deadline),
    or **withdrawn** by the claimant, and the slot is subsequently reopened
    after the 3-day appeal window. Retained fees remain in the fee escrow to
    offset reviewer and operational overhead.

---

## 4. Round Sizing, Deadlines, and Reopen Limits

| Parameter | Field / Constant | Default / Rule |
|---|---|---|
| **Attempts per round** | `attempts_per_round` | Fixed at `N = 3` paid slots per round (`BOUNTY_ATTEMPTS_PER_ROUND = 3`). |
| **Entry fee vs. reward pool** | `entry_fee_alpha_rao` / `reward_pool_alpha_rao` | Entry fee must be positive and strictly less than or equal to 25% of `reward_pool_alpha_rao` (recommended 2%–10% of pool) to deter slot squatting without pricing out contributors. |
| **Unpaid hold window** | `hold_minutes` | Default `30` minutes (`1` to `1440` minutes). |
| **Delivery deadline** | `delivery_days` | Default `5` days (`1` to `90` days) from entry-fee confirmation. |
| **Review SLA** | `review_days` | Default `7` days (`1` to `30` days) from each signed submission. Overdue reviews never penalize the contributor. |
| **Appeal window** | `BOUNTY_APPEAL_WINDOW` | `3` days from rejection or recorded missed deadline before a slot may be reopened. |
| **Maximum reopens** | Pilot cap | Closed slots are reopened individually by an operator (`control_bounty_slot`); nothing reopens automatically. |

---

## 5. Governance Roles & Separation of Duties

| Role | Responsibilities | Conflict-of-Interest / Recusal Rules |
|---|---|---|
| **Bounty Author** | Drafts `BountyTerms`, scope, interfaces, and acceptance evidence; publishes the revision via `publish_bounty_revision`. | Cannot claim a slot on the bounty if also named as `reviewer_login`. |
| **Assigned Reviewer** (`reviewer_login` / `reviewer_actor`) | Evaluates signed submissions against `acceptance_evidence` and records `accept`, `changes_requested`, or `reject` with public rationale. | Refused from claiming a slot on the bounty (`403`); cannot be assigned as reviewer if holding a live claim (`409`); **cannot decide an appeal** of their own rejection or rework deadline (`403`); **cannot approve the payout** of an obligation they accepted (`403`). |
| **Appeal Handler / Tie-Breaker** | Reviews private appeal statements and records `upheld` (with `restore_days`) or `denied` via `decide_bounty_appeal`. | Must be a distinct operator from the reviewer who rejected the submission or set the missed rework/restored deadline (`403`). |
| **Treasury Operator / Approver** | Configures payout policy, awards accepted claims (`award_bounty_claim`), approves obligations (`approve_bounty_obligation`), dispatches transfers, and reconciles chain receipts. | Whoever decided the award (`obligation.actor`) or accepted the submission (`reviewer_actor`) **cannot approve** the obligation's payout (`403`). |

---

## 6. Public Transparency Contract

Every bounty on the public board (`/api/v1/public/bounties`) and claim view
(`/api/v1/bounties/claims/{claim_id}`) exposes:
- Every published revision (`spec_digest`, `terms`, `published_at`).
- Every round and slot with its effective status, deadlines, and claimant
  identity (`github_login`, `hotkey`, `payee_coldkey`, signed `revision`).
- Every entry-fee receipt and refund obligation.
- Every signed submission (`pr_number`, `head_sha`, `submitted_at`) and review
  decision (`decision`, public `reason`, `attribution`, `decided_at`).
- Every appeal's `reason_digest`, `subject_event_id`, and decision outcome.
- Every award and refund obligation, approval counts (`approvals` /
  `approvals_required`), transfer state, chain receipt, and delivery facts
  (`merged`, `deployed`, `adopted`).

Private audit fields (operator emails, private appeal bodies, internal pause/
cancel/award operator notes) remain restricted to Backroom operators.

---

## 7. Anti-Abuse Rules

1. **Sybil & Slot Squatting Protection**:
   - Claiming a slot requires an on-chain finalized hotkey (`Owner(hotkey) == payee_coldkey`), an active verified GitHub link (`ditto link-github`), a current signed CLA (`ditto cla sign`), and a confirmed alpha entry fee within `hold_minutes`.
   - At most **one live slot** per GitHub account and per `payee_coldkey` on any bounty.
2. **Self-Bounty & Insider Restrictions**:
   - Case-insensitive enforcement ensures `reviewer_login` can never hold a claim on the bounty they review.
   - Staff and operators are prohibited from claiming bounties they authored, reviewed, or administer.
   - Payout approvals enforce strict recusal of both the accepting reviewer and the award decider.
3. **AI-Generated Spam & Low-Effort Submissions**:
   - Submissions must bind an exact 40-char commit SHA (`head_sha`) on a real pull request in the target repository.
   - Submissions that fail objective `acceptance_evidence` checks or ignore `public_interfaces` are rejected, and upon slot reopen after the appeal window, their entry fee is retained (`fee_retained`).
4. **Conflict-of-Interest Disclosure**:
   - Any reviewer with a prior financial or collaborative relationship with a claimant must request reassignment via `reassign_bounty_reviewer` before recording a review decision.

---

## 8. Sample Build-Bounty Template

```yaml
repo: "ditto-assistant/ditto-subnet"
issue: 2942
terms:
  title: "Implement deterministic bounty specification validator"
  scope: "Add specification validation helpers in apps/platform/ditto/bounty_spec_validator.py and unit tests."
  public_interfaces: "validate_bounty_spec(spec: dict) -> list[str]"
  acceptance_evidence: "uv run pytest apps/platform/ditto/tests/test_bounty_spec_validator.py passes with 100% exit code 0."
  dependencies: []
  reviewer_login: "ditto-reviewer"
  entry_fee_alpha_rao: 2000000000
  reward_pool_alpha_rao: 100000000000
  attempts_per_round: 3
  delivery_days: 5
  review_days: 7
  hold_minutes: 30
  funding_mode: "rehearsal"
  fee_escrow_coldkey: "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
  fee_escrow_hotkey: "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
```
