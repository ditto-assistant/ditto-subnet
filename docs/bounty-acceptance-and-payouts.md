# SN118 bounty acceptance, payout proof, disputes, and public accounting

Status: proposed. This is the acceptance, payout verification, dispute resolution, and public accounting contract for the SN118 maintenance treasury specified in [maintenance-treasury.md](maintenance-treasury.md) and [bounty-claims.md](bounty-claims.md). Parent epic: #2054. Related issues: #2044 (treasury governance and accounting), #2045 (hotkey-signed claims and contributor identity), and #2046 (acceptance, payout proof, disputes, and public accounting).

The lifecycle bridges completed technical work to on-chain disbursement while maintaining strict separation of concerns across submission, code review, merging, deployment verification, multi-operator approval, and chain settlement.

## Core principles

1. **Merge alone cannot trigger payment.** Code merge is a necessary technical milestone, not a financial authorization. Payment requires independent deployment verification, formal multi-reviewer approval binding the exact commit hash, and verified on-chain extrinsic proof.
2. **Deterministic approval binding.** Approval records bind the repository target, issue number, exact commit SHA, signed claimant hotkey, payee coldkey, bounty reward revision, amount in rao, and governing treasury policy revision into a single immutable, checksummed payload.
3. **Multi-operator governance thresholds.** Disbursing funds above configured amounts requires multiple distinct operator signatures. No single operator can unilaterally authorize large payouts.
4. **On-chain extrinsic verification.** Every payout traces to a confirmed Bittensor chain extrinsic (`Balances.transfer_keep_alive` with `ExtrinsicSuccess`) whose canonical block hash, recipient, and rao amount match the approved record.
5. **Tamper-evident public accounting.** All lifecycle state transitions, approvals, and chain receipts append to an immutable, hash-chained ledger (`prev_hash` -> `entry_hash`) anchored at `GENESIS_HASH`. Anyone can replay and independently verify ledger integrity.
6. **Source-safe transparency.** Public audit projections expose verified work, claim history, and chain receipts while redacting private reviewer notes, internal commentary, and operational secrets.

## Lifecycle state machine

Bounty work progresses through seven linear states, complemented by four exception and terminal dispute states.

```
[ CLAIMED ] 
     │
     ▼
[ SUBMITTED ] ──────► [ REJECTED ] ──► [ APPEALED ]
     │                      │                 │
     ▼                      ▼                 ▼
[ ACCEPTED ]          [ CANCELLED ]    (Re-reviewed)
     │
     ▼
 [ MERGED ]  <── (Technical merge on GitHub)
     │
     ▼
[ DEPLOYED_VERIFIED ]  <── (Canary / automated verification)
     │
     ▼
[ APPROVED ]  <── (Multi-operator threshold approval)
     │
     ▼
  [ PAID ]  <── (On-chain extrinsic proof verified)
```

### State definitions

| State | Prerequisites | Mutability | Description |
|---|---|---|---|
| `claimed` | Active reservation with valid sr25519 signature | Mutable | Contributor holds exclusive or shared reservation on issue |
| `submitted` | PR opened and exact commit `head_sha` submitted | Mutable | Deliverables ready for technical and policy review |
| `accepted` | Technical review passed against acceptance evidence | Locked | Reviewer confirms work meets bounty requirements |
| `merged` | Pull request merged into canonical branch | Locked | Code merged; PR merge commit hash recorded |
| `deployed_verified` | Live runtime / canary / testnet verification passed | Locked | Deployed artifacts verified in operational environment |
| `approved` | N-of-M distinct operator approval signatures collected | Immutable | Financial disbursement authorized with pinned amount and payee |
| `paid` | Valid on-chain extrinsic proof verified and ledgered | Terminal | Transaction settled and linked to block hash and extrinsic index |
| `rejected` | Review failed or acceptance evidence not satisfied | Mutable | Contributor notified with structured rejection reason |
| `appealed` | Rejection contested with justification | Mutable | Escalated to independent operator panel for re-review |
| `cancelled` | Reservation expired, withdrawn, or scope decommissioned | Terminal | Work unassigned and funds returned to unallocated pool |
| `handed_off` | Dual-signed transfer to successor contributor | Terminal | Work and claim continuity passed to successor |
| `payout_failed` | Chain extrinsic failed, reverted, or timed out | Exception | Payout halted for investigation; auto-retry is forbidden |

### Invariant: merge alone cannot trigger payment

The transition from `merged` directly to `paid` is prohibited across both application logic and database schema constraints. A valid payout record requires:
- `status == 'paid'`
- `merged_at IS NOT NULL`
- `deployed_verified_at IS NOT NULL`
- `approved_at IS NOT NULL`
- `paid_at IS NOT NULL`
- `tx_receipt IS NOT NULL`

If a PR is merged without subsequent deployment verification and formal multi-operator approval, no disbursement can occur.

## Approval binding and checksumming

Before any funds can move, maintainers generate an immutable `BountyApprovalRecord`. The record binds:

1. `repo`: Canonical target repository (e.g. `ditto-assistant/ditto-subnet`)
2. `issue`: Target issue number
3. `claim_id`: Unique UUID of the active claim
4. `commit_sha`: Exact 40-character hexadecimal git commit SHA of accepted deliverables
5. `claimant_hotkey`: SS58 address of registered contributor hotkey
6. `payee_coldkey`: SS58 address of verified destination coldkey
7. `reward_revision`: Specific bounty specification revision number
8. `amount_rao`: Authorized payout amount in rao (1 TAO = 1,000,000,000 rao)
9. `amount_tao`: TAO representation of payout amount
10. `treasury_policy_revision`: Governing treasury policy revision
11. `split_shares`: List of team allocations (if team claim)
12. `approvals`: List of distinct operator signatures and roles
13. `confirmation_phrase`: Typed confirmation phrase: `APPROVE BOUNTY PAYOUT`
14. `checksum`: SHA-256 digest of canonical JSON serialization of fields 1 through 12

Any modification to the commit hash, recipient coldkey, or amount invalidates the approval checksum and aborts payout execution.

## Multi-reviewer governance thresholds

Disbursement approvals enforce tiered multi-operator consensus based on payout magnitude:

| Tier | Payout Range (TAO) | Payout Range (rao) | Threshold Requirement |
|---|---|---|---|
| **Tier 1 (Small)** | `<= 5.0 TAO` | `<= 5,000,000,000 rao` | 1 authorized operator review and approval |
| **Tier 2 (Medium)** | `> 5.0` and `<= 25.0 TAO` | `> 5,000,000,000` and `<= 25,000,000,000 rao` | Minimum 2 distinct authorized operator approvals |
| **Tier 3 (Large)** | `> 25.0 TAO` | `> 25,000,000,000 rao` | Minimum 3 distinct authorized operator approvals (`ENDORSEMENT_THRESHOLD = 3`) |

Rules:
- Each operator signature must originate from a distinct, authorized operator hotkey registered in treasury settings.
- Duplicate signatures from the same operator on the same approval record are rejected.
- Large payouts exceeding 25 TAO must satisfy the same 3-endorsement threshold enforced by `NameClaim` consensus.

## On-chain payout proof verification

Payout proof verification operates in reverse to incoming upload fee verification. The verifier validates the outbound Substrate extrinsic emitted by the treasury multisig coldkey:

1. **Block hash confirmation**: The verifier queries the chain RPC with `block_number` to obtain the finalized canonical block hash. The canonical block hash must match `proof.block_hash`.
2. **Extrinsic retrieval**: Retrieves the extrinsic at `(block_number, extrinsic_index)`.
3. **Call type validation**: The extrinsic call must be `Balances.transfer_keep_alive` or `Balances.transfer`. `transfer_keep_alive` is preferred to prevent account reaping.
4. **Execution success**: Confirms the block events contain `System.ExtrinsicSuccess` at the matching extrinsic index. If `System.ExtrinsicFailed` was emitted, verification fails immediately.
5. **Destination coldkey validation**: Call argument `dest` must decode to an SS58 address matching `payee_coldkey` recorded in the approval record.
6. **Disbursement amount validation**: Call argument `value` must equal `amount_rao` recorded in the approval record (or the exact allocated share in a split payment).
7. **Signer validation**: The extrinsic signer must match one of the authorized treasury disbursement coldkeys.

### Double payment and replay prevention

Double payment is prevented at multiple layers:
1. **Extrinsic deduplication**: A unique constraint on `(block_hash, extrinsic_index)` prevents the same on-chain transaction from being submitted or credited more than once.
2. **Deliverable deduplication**: A unique constraint on `(repo, issue, commit_sha)` ensures the exact same work product cannot be approved or paid twice across duplicate issues or rewritten pull requests.
3. **Claim isolation**: Each payout proof references a specific `claim_id`. Once marked `paid`, a claim is terminal and cannot accept additional proofs.

## Partial and shared awards

Collaborative efforts and team claims support partial or shared awards:
- An approval record can define a list of `split_shares`.
- Each split share specifies:
  - `claimant_hotkey`: Contributing member's hotkey
  - `payee_coldkey`: Destination coldkey for that member's share
  - `amount_rao`: Individual allocation in rao
  - `share_ratio`: Proportion of total award
- **Strict Conservation Invariant**: `sum(share.amount_rao for share in split_shares) == total_approved_amount_rao`.
- Each share is settled with an independent on-chain extrinsic proof. The parent bounty transitions to `paid` only after all split shares have verified receipts.

## Disputes, appeals, and lifecycle exceptions

1. **Rejection**: If deliverables fail acceptance testing or violate treasury guidelines, the reviewer marks the claim `rejected` and records a structured reason digest in the ledger.
2. **Appeal**: The contributor may submit a signed appeal (`ditto-bounty-appeal:v1`) within 7 days. Appeals trigger assignment of an independent operator panel for re-review.
3. **Cancellation**: If a contributor withdraws, abandons work, or fails to renew before reservation expiration, the claim transitions to `cancelled`. Reserved funds are released back to the unallocated treasury balance.
4. **Handoff**: A contributor unable to complete work may execute a cryptographic handoff to a successor contributor. Both parties must submit matching signed handoff halves (`ditto-bounty-handoff:v1`).
5. **Key rotation**: If a contributor rotates their coldkey before payout, they must submit a payee rebind action signed by the verified on-chain owner coldkey. Rebinds cannot occur after approval without re-approval by operators.
6. **Failed transaction**: If a payout transaction fails on-chain, the record is marked `payout_failed`. Automatic retries are forbidden; an operator must diagnose the root cause (e.g. nonce collision, fee spike) before reissuing.

## Tamper-evident hash-chained accounting ledger

All bounty operations append to `bounty_audit_log`, a tamper-evident, append-only hash chain modeled on `score_audit_log`:

- `seq`: Monotonically increasing sequence number (BIGSERIAL).
- `prev_hash`: SHA-256 hex digest of the previous entry (`"0" * 64` for genesis).
- `entry_hash`: SHA-256 hex digest of this entry's canonical content (embedding `prev_hash`).
- `payload`: Structured immutable event payload.
- `recorded_at`: UTC timestamp.

### Verification algorithm

Any external participant or auditor can verify ledger integrity:
```python
def verify_bounty_ledger_chain(entries: list[BountyLedgerEntry], expected_prev: str = GENESIS_HASH) -> bool:
    running = expected_prev
    for entry in entries:
        if entry.prev_hash != running:
            return False
        if compute_entry_hash(entry.canonical_dict()) != entry.entry_hash:
            return False
        running = entry.entry_hash
    return True
```

## Source-safe public accounting

Public audit queries (`/api/v1/bounties/public/audit`) project verified public records without compromising internal security:

- **Published data**: Repository, issue number, PR number, merged commit SHA, claimant hotkey, payee coldkey, amount paid (rao and TAO), block hash, extrinsic index, and state transition history.
- **Redacted data**: Internal reviewer notes, private chat logs, candidate identity disclosures, and operator API tokens.

This ensures complete financial and technical accountability on public infrastructure while maintaining contributor privacy and network security.
