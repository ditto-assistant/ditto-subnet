# SN118 Maintenance Treasury and Miner Bounty Operations Runbook

Status: **production specification**. This document provides the unified operational runbook for executing the 5% maintenance treasury and miner bounty operations lifecycle specified in parent epic #2054 and sub-issues #2044, #2045, #2046, and #2047.

## Executive Outcome

Fund meaningful SN118 maintenance through a transparent 5% treasury with scoped GitHub bounties, signed contributor claims, objective acceptance, and chain-reconciled payouts.

## Architectural Component Matrix

The bounty operations engine integrates four governance and technical subsystems:

| Layer | Issue | Implementation Scope | Primary Reference |
|---|---|---|---|
| Governance & Treasury | #2044 | 5% emission share, custody keys, conflict disclosures, threat models, reversible caps | `docs/maintenance-treasury.md` |
| Contributor Identity | #2045 | Hotkey-signed claims, domain tags, replay protection, coldkey owner routing | `docs/bounty-claims.md` |
| Acceptance & Payout | #2046 | Multi-operator approvals, deployment verification, hash-chained ledger, disputes | `docs/bounty-acceptance-and-payouts.md` |
| Board & Workflow | #2047 | GitHub label schema, issue templates, automated lifecycle actions, contributor guide | `docs/CONTRIBUTING_BOUNTIES.md` |

## Strict Operational Invariants

1. **Discussion is not a payment promise.** Discord conversations, GitHub comments, issue assignments, or preliminary reviews never constitute a financial commitment.
2. **Merge alone cannot trigger payment.** Code merge on GitHub is a necessary engineering step, not an authorization for disbursement.
3. **True verification over mocks.** Cryptographic signatures and state assertions must use authentic primitives (sr25519/ed25519, SHA-256 digests). Bypassing assertions or mocking on-chain balances is prohibited.
4. **Independent deployment verification required.** Payout authorization requires verified canary or production deployment health checks.
5. **Exact commit SHA binding.** Payout approvals bind the exact Git commit SHA containing the accepted deliverable. Altering code after approval invalidates the authorization.
6. **Multi-operator approval thresholds.** Payouts exceeding 5.0 TAO require at least two distinct, non-conflicted operator approvals.
7. **On-chain extrinsic reconciliation.** Payments execute exclusively via confirmed Bittensor transactions (`Balances.transfer_keep_alive` or `Balances.transfer`) matching the approved payee coldkey and amount.
8. **Double-spend protection.** On-chain settlement receipts index unique `(block_hash, extrinsic_index)` pairs, permanently preventing duplicate disbursements.
9. **Tamper-evident hash-chained ledger.** All state transitions append to an immutable ledger anchored at `GENESIS_HASH` (`prev_hash` -> `entry_hash`).

## Five-Stage Activation Order

Operations must advance sequentially through five defined stages. Bypassing or reordering stages is prohibited.

```
[ STAGE 1: Governance & Custody Approval ]
                    │
                    ▼
[ STAGE 2: Claim & Acceptance Contracts Publication ]
                    │
                    ▼
[ STAGE 3: Board & Ledger Rehearsal (Zero Funds) ]
                    │
                    ▼
[ STAGE 4: Capped Treasury Allocation Activation ]
                    │
                    ▼
[ STAGE 5: Accepted Work Payout & Receipt Publication ]
```

### Stage 1: Governance, Funding Source, Custody, and Accounting

1. Maintainers review and formally approve `docs/maintenance-treasury.md`.
2. Select funding mechanism: 5% cut folded into validator miner incentive vectors onto the designated treasury hotkey (`miner_vector_cut`).
3. Register custody keys:
   - Designated treasury coldkey for multi-signature fund custody.
   - Registered treasury hotkey for subnet miner weight vector participation.
4. Publish conflict disclosures for all participating maintainers and reviewers.

### Stage 2: Publish Claim and Acceptance Contracts

1. Deploy platform API models and verification engines:
   - Wire models: `ditto/api_models/bounty_claim.py`, `ditto/api_models/bounty_payout.py`.
   - Platform backend: `apps/platform/ditto/api_server/bounty_claim.py`, `apps/platform/ditto/api_server/bounty_payout.py`.
2. Publish contributor contracts:
   - Hotkey claim specification: `docs/bounty-claims.md`.
   - Acceptance and accounting contract: `docs/bounty-acceptance-and-payouts.md`.
3. Standardize claim payload format:
   `ditto-bounty-claim:v1:{netuid}:{repo}:{issue}:{spec_digest}:{claimant_hotkey}:{payee_coldkey}:{nonce}:{expires_at}`.

### Stage 3: Run the Board Without Funds and Rehearse the Ledger

1. Synchronize repository label taxonomy using `scripts/sync_bounty_labels.py`.
2. Publish contributor guidelines (`docs/CONTRIBUTING_BOUNTIES.md`) and issue template (`.github/ISSUE_TEMPLATE/bounty.md`).
3. Execute dry-run rehearsal using the zero-fund simulation CLI:
   ```bash
   python3 scripts/rehearse_bounty_ledger.py --repo ditto-assistant/ditto-subnet --issue 2054
   ```
4. Verify that the simulation exercises every state transition:
   - `claimed` -> `submitted` -> `accepted` -> `merged` -> `deployed_verified` -> `approved` -> `paid (shadow)`
5. Verify ledger hash chain integrity from `GENESIS_HASH` to head.
6. Verify invariant check: direct transition from `merged` to `paid` throws `MergeCannotTriggerPaymentError`.

### Stage 4: Activate a Capped Treasury Allocation

1. Initialize live treasury configuration with a conservative, reversible budget ceiling:
   - Initial epoch cap: 10.0 TAO.
   - Single bounty maximum: 25.0 TAO.
   - Multi-reviewer threshold: 5.0 TAO.
2. Verify rate-limiting and budget cap enforcement in `ditto/bounty/treasury.py`.
3. Verify that emergency pause controls are operational (`emergency_pause` / `resume`).

### Stage 5: Pay Only Against Accepted Exact Work and Publish Receipt

1. Contributor submits deliverable with signed submit payload and commit SHA.
2. Reviewer conducts technical review, verifies automated tests, and issues acceptance.
3. Code merges to `main`.
4. Independent automated verification confirms canary or production deployment health.
5. Authorized operators sign `BountyApprovalRecord` binding the commit SHA, claimant hotkey, and payee coldkey.
6. Treasury coldkey executes `Balances.transfer_keep_alive` on Subtensor.
7. System reconciles extrinsic hash, block hash, and amount against the approval record.
8. System records `BountyPayoutReceiptRecord` and generates public settlement receipt:
   ```bash
   python3 scripts/rehearse_bounty_ledger.py --output docs/receipts/2054-settlement.md
   ```

## Emergency Procedures

### Emergency Pause
If an operational defect, unauthorized claim activity, or contract ambiguity is identified, any authorized operator can halt all disbursements immediately:
```python
treasury.emergency_pause(reason="Investigating reconciliation variance", operator_hotkey="<HOTKEY>")
```

### Unpausing Operations
Resuming disbursements requires explicit operator action following root-cause resolution:
```python
treasury.resume(operator_hotkey="<HOTKEY>")
```
