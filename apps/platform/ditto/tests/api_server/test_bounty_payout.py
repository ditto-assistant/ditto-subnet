"""Unit tests for bounty acceptance, payout proofs, disputes, and public accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import bittensor
import pytest

from ditto.api_models.bounty_payout import (
    BountyApprovalRecord,
    BountyPayoutProof,
    BountySplitShare,
    OperatorApproval,
)
from ditto.api_server.bounty_payout import (
    CONFIRMATION_PHRASE,
    GENESIS_HASH,
    BountyChainPayoutVerifier,
    BountyLedger,
    BountyStateMachine,
    DuplicateApproverError,
    DuplicatePaymentError,
    InsufficientApprovalsError,
    InvalidPayoutProofError,
    InvalidSplitSharesError,
    InvalidStateTransitionError,
    MergeCannotTriggerPaymentError,
    compute_approval_checksum,
    export_public_audit_record,
    required_approvals_for_amount,
    validate_operator_approvals,
    validate_split_shares,
    verify_bounty_ledger_chain,
)

_CLAIM_ID = UUID("11111111-2222-3333-4444-555555555555")
_BLOCK_HASH = "a" * 64
_COMMIT_SHA = "b" * 40
_SIG_HEX = "c" * 128


@dataclass(frozen=True)
class MockExtrinsic:
    """Mock Substrate extrinsic object for testing chain verification."""

    call_module: str
    call_function: str
    call_args: dict[str, Any]
    signer_address: str


class MockChainClient:
    """Mock Bittensor chain RPC client for reverse payout verification."""

    def __init__(
        self,
        *,
        canonical_block_hash: str = _BLOCK_HASH,
        extrinsic: MockExtrinsic | None = None,
        is_success: bool = True,
        block_timestamp_seconds: int = 1758456000,
    ) -> None:
        self.canonical_block_hash = canonical_block_hash
        self.extrinsic = extrinsic
        self.is_success = is_success
        self.block_timestamp_seconds = block_timestamp_seconds

    async def get_block_hash(self, _block_number: int) -> str:
        """Return configured block hash."""
        return self.canonical_block_hash

    async def get_extrinsic(
        self, _block_number: int, _extrinsic_index: int
    ) -> MockExtrinsic:
        """Return configured extrinsic."""
        if self.extrinsic is None:
            raise RuntimeError("No extrinsic configured")
        return self.extrinsic

    async def check_extrinsic_success(
        self, _block_hash: str, _extrinsic_index: int
    ) -> bool:
        """Return configured execution outcome."""
        return self.is_success

    async def get_block_timestamp(self, _block_hash: str) -> int:
        """Return configured block timestamp."""
        return self.block_timestamp_seconds


def test_merge_alone_cannot_trigger_payment() -> None:
    """Ensure transitioning directly from merged to paid is strictly blocked."""
    sm = BountyStateMachine("claimed")
    sm.transition_to("submitted")
    sm.transition_to("accepted")
    sm.transition_to("merged")

    with pytest.raises(MergeCannotTriggerPaymentError):
        sm.transition_to("paid")

    sm.transition_to("deployed_verified")
    sm.transition_to("approved")
    sm.transition_to("paid")
    assert sm.current_state == "paid"


def test_invalid_lifecycle_transitions() -> None:
    """Ensure out-of-order state transitions raise errors."""
    sm = BountyStateMachine("claimed")
    with pytest.raises(InvalidStateTransitionError):
        sm.transition_to("approved")

    with pytest.raises(InvalidStateTransitionError):
        sm.transition_to("deployed_verified")


def test_dispute_and_appeal_lifecycle() -> None:
    """Test full rejection, appeal, and re-acceptance cycle."""
    sm = BountyStateMachine("claimed")
    sm.transition_to("submitted")
    sm.transition_to("rejected")
    sm.transition_to("appealed")
    sm.transition_to("accepted")
    assert sm.current_state == "accepted"


def test_handoff_and_cancellation() -> None:
    """Test handoff and cancellation terminal states."""
    sm1 = BountyStateMachine("claimed")
    sm1.transition_to("handed_off")
    assert sm1.current_state == "handed_off"

    sm2 = BountyStateMachine("claimed")
    sm2.transition_to("submitted")
    sm2.transition_to("cancelled")
    assert sm2.current_state == "cancelled"


def test_payout_failed_recovery() -> None:
    """Test payout failure transition without auto-retry."""
    sm = BountyStateMachine("claimed")
    sm.transition_to("submitted")
    sm.transition_to("accepted")
    sm.transition_to("merged")
    sm.transition_to("deployed_verified")
    sm.transition_to("approved")
    sm.transition_to("payout_failed")
    assert sm.current_state == "payout_failed"

    sm.transition_to("paid")
    assert sm.current_state == "paid"


def test_multi_reviewer_threshold_tiers() -> None:
    """Verify tier calculations and multi-operator approval requirements."""
    assert required_approvals_for_amount(1_000_000_000) == 1
    assert required_approvals_for_amount(5_000_000_000) == 1
    assert required_approvals_for_amount(5_000_000_001) == 2
    assert required_approvals_for_amount(25_000_000_000) == 2
    assert required_approvals_for_amount(25_000_000_001) == 3

    alice = bittensor.Keypair.create_from_uri("//Alice")
    bob = bittensor.Keypair.create_from_uri("//Bob")
    charlie = bittensor.Keypair.create_from_uri("//Charlie")
    now = datetime.now(UTC)

    app_alice = OperatorApproval(
        operator_hotkey=alice.ss58_address,
        signature=_SIG_HEX,
        role="approver",
        approved_at=now,
    )
    app_bob = OperatorApproval(
        operator_hotkey=bob.ss58_address,
        signature=_SIG_HEX,
        role="approver",
        approved_at=now,
    )
    app_charlie = OperatorApproval(
        operator_hotkey=charlie.ss58_address,
        signature=_SIG_HEX,
        role="lead",
        approved_at=now,
    )

    validate_operator_approvals(3_000_000_000, [app_alice])

    with pytest.raises(InsufficientApprovalsError):
        validate_operator_approvals(10_000_000_000, [app_alice])

    validate_operator_approvals(10_000_000_000, [app_alice, app_bob])

    with pytest.raises(InsufficientApprovalsError):
        validate_operator_approvals(30_000_000_000, [app_alice, app_bob])

    validate_operator_approvals(30_000_000_000, [app_alice, app_bob, app_charlie])


def test_duplicate_approver_rejection() -> None:
    """Ensure duplicate approvals from the same operator are rejected."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    now = datetime.now(UTC)
    app1 = OperatorApproval(
        operator_hotkey=alice.ss58_address,
        signature=_SIG_HEX,
        role="approver",
        approved_at=now,
    )
    app2 = OperatorApproval(
        operator_hotkey=alice.ss58_address,
        signature=_SIG_HEX,
        role="reviewer",
        approved_at=now,
    )

    with pytest.raises(DuplicateApproverError):
        validate_operator_approvals(10_000_000_000, [app1, app2])


def test_split_shares_conservation() -> None:
    """Verify team split shares must strictly conserve the approved amount."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    bob = bittensor.Keypair.create_from_uri("//Bob")

    share_alice = BountySplitShare(
        claimant_hotkey=alice.ss58_address,
        payee_coldkey=alice.ss58_address,
        amount_rao=6_000_000_000,
        share_ratio=0.6,
    )
    share_bob = BountySplitShare(
        claimant_hotkey=bob.ss58_address,
        payee_coldkey=bob.ss58_address,
        amount_rao=4_000_000_000,
        share_ratio=0.4,
    )

    validate_split_shares(10_000_000_000, [share_alice, share_bob])

    invalid_share_bob = BountySplitShare(
        claimant_hotkey=bob.ss58_address,
        payee_coldkey=bob.ss58_address,
        amount_rao=3_000_000_000,
        share_ratio=0.3,
    )
    with pytest.raises(InvalidSplitSharesError):
        validate_split_shares(10_000_000_000, [share_alice, invalid_share_bob])


def test_approval_record_binding_and_checksum() -> None:
    """Verify cryptographic binding and tampering detection in approval records."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    op = bittensor.Keypair.create_from_uri("//Bob")
    now = datetime.now(UTC)

    approvals = [
        OperatorApproval(
            operator_hotkey=op.ss58_address,
            signature=_SIG_HEX,
            role="approver",
            approved_at=now,
        )
    ]

    base_data: dict[str, Any] = {
        "repo": "ditto-assistant/ditto-subnet",
        "issue": 2046,
        "claim_id": _CLAIM_ID,
        "commit_sha": _COMMIT_SHA,
        "claimant_hotkey": alice.ss58_address,
        "payee_coldkey": alice.ss58_address,
        "reward_revision": 1,
        "amount_rao": 5_000_000_000,
        "amount_tao": 5.0,
        "treasury_policy_revision": 1,
        "split_shares": [],
        "approvals": approvals,
        "confirmation_phrase": CONFIRMATION_PHRASE,
    }

    checksum = compute_approval_checksum(base_data)
    record = BountyApprovalRecord(**base_data, checksum=checksum, approved_at=now)
    assert record.checksum == checksum

    tampered_data = dict(base_data)
    tampered_data["commit_sha"] = "0" * 40
    assert compute_approval_checksum(tampered_data) != checksum


@pytest.mark.asyncio
async def test_on_chain_payout_verifier_happy_path() -> None:
    """Test successful verification of valid Bittensor chain extrinsic."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    treasury = bittensor.Keypair.create_from_uri("//Charlie")
    op = bittensor.Keypair.create_from_uri("//Bob")
    now = datetime.now(UTC)

    approvals = [
        OperatorApproval(
            operator_hotkey=op.ss58_address,
            signature=_SIG_HEX,
            role="approver",
            approved_at=now,
        )
    ]

    base_data: dict[str, Any] = {
        "repo": "ditto-assistant/ditto-subnet",
        "issue": 2046,
        "claim_id": _CLAIM_ID,
        "commit_sha": _COMMIT_SHA,
        "claimant_hotkey": alice.ss58_address,
        "payee_coldkey": alice.ss58_address,
        "reward_revision": 1,
        "amount_rao": 5_000_000_000,
        "amount_tao": 5.0,
        "treasury_policy_revision": 1,
        "split_shares": [],
        "approvals": approvals,
        "confirmation_phrase": CONFIRMATION_PHRASE,
    }
    checksum = compute_approval_checksum(base_data)
    approval = BountyApprovalRecord(**base_data, checksum=checksum, approved_at=now)

    mock_extrinsic = MockExtrinsic(
        call_module="Balances",
        call_function="transfer_keep_alive",
        call_args={"dest": alice.ss58_address, "value": 5_000_000_000},
        signer_address=treasury.ss58_address,
    )
    client = MockChainClient(
        canonical_block_hash=_BLOCK_HASH,
        extrinsic=mock_extrinsic,
        is_success=True,
    )

    verifier = BountyChainPayoutVerifier(
        authorized_treasury_coldkeys={treasury.ss58_address}
    )
    proof = BountyPayoutProof(
        block_number=100,
        block_hash=_BLOCK_HASH,
        extrinsic_index=2,
        claim_id=_CLAIM_ID,
        payee_coldkey=alice.ss58_address,
        amount_rao=5_000_000_000,
    )

    receipt = await verifier.verify_payout(
        chain_client=client,
        proof=proof,
        approval=approval,
        now=now,
    )
    assert receipt.block_hash == _BLOCK_HASH
    assert receipt.extrinsic_index == 2
    assert receipt.amount_rao == 5_000_000_000
    assert receipt.treasury_coldkey == treasury.ss58_address


@pytest.mark.asyncio
async def test_on_chain_verifier_rejection_cases() -> None:
    """Test all failure conditions for chain extrinsic validation."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    eve = bittensor.Keypair.create_from_uri("//Eve")
    treasury = bittensor.Keypair.create_from_uri("//Charlie")
    op = bittensor.Keypair.create_from_uri("//Bob")
    now = datetime.now(UTC)

    base_data: dict[str, Any] = {
        "repo": "ditto-assistant/ditto-subnet",
        "issue": 2046,
        "claim_id": _CLAIM_ID,
        "commit_sha": _COMMIT_SHA,
        "claimant_hotkey": alice.ss58_address,
        "payee_coldkey": alice.ss58_address,
        "reward_revision": 1,
        "amount_rao": 5_000_000_000,
        "amount_tao": 5.0,
        "treasury_policy_revision": 1,
        "split_shares": [],
        "approvals": [
            OperatorApproval(
                operator_hotkey=op.ss58_address,
                signature=_SIG_HEX,
                role="approver",
                approved_at=now,
            )
        ],
        "confirmation_phrase": CONFIRMATION_PHRASE,
    }
    checksum = compute_approval_checksum(base_data)
    approval = BountyApprovalRecord(**base_data, checksum=checksum, approved_at=now)

    verifier = BountyChainPayoutVerifier(
        authorized_treasury_coldkeys={treasury.ss58_address}
    )

    proof = BountyPayoutProof(
        block_number=100,
        block_hash=_BLOCK_HASH,
        extrinsic_index=1,
        claim_id=_CLAIM_ID,
        payee_coldkey=alice.ss58_address,
        amount_rao=5_000_000_000,
    )

    client_bad_hash = MockChainClient(
        canonical_block_hash="0" * 64,
        extrinsic=MockExtrinsic(
            call_module="Balances",
            call_function="transfer_keep_alive",
            call_args={"dest": alice.ss58_address, "value": 5_000_000_000},
            signer_address=treasury.ss58_address,
        ),
    )
    with pytest.raises(InvalidPayoutProofError):
        await verifier.verify_payout(
            chain_client=client_bad_hash, proof=proof, approval=approval
        )

    client_bad_call = MockChainClient(
        extrinsic=MockExtrinsic(
            call_module="System",
            call_function="remark",
            call_args={"dest": alice.ss58_address, "value": 5_000_000_000},
            signer_address=treasury.ss58_address,
        )
    )
    with pytest.raises(InvalidPayoutProofError):
        await verifier.verify_payout(
            chain_client=client_bad_call, proof=proof, approval=approval
        )

    client_failed_ext = MockChainClient(
        extrinsic=MockExtrinsic(
            call_module="Balances",
            call_function="transfer_keep_alive",
            call_args={"dest": alice.ss58_address, "value": 5_000_000_000},
            signer_address=treasury.ss58_address,
        ),
        is_success=False,
    )
    with pytest.raises(InvalidPayoutProofError):
        await verifier.verify_payout(
            chain_client=client_failed_ext, proof=proof, approval=approval
        )

    client_wrong_dest = MockChainClient(
        extrinsic=MockExtrinsic(
            call_module="Balances",
            call_function="transfer_keep_alive",
            call_args={"dest": eve.ss58_address, "value": 5_000_000_000},
            signer_address=treasury.ss58_address,
        )
    )
    with pytest.raises(InvalidPayoutProofError):
        await verifier.verify_payout(
            chain_client=client_wrong_dest, proof=proof, approval=approval
        )

    client_wrong_amount = MockChainClient(
        extrinsic=MockExtrinsic(
            call_module="Balances",
            call_function="transfer_keep_alive",
            call_args={"dest": alice.ss58_address, "value": 1_000_000},
            signer_address=treasury.ss58_address,
        )
    )
    with pytest.raises(InvalidPayoutProofError):
        await verifier.verify_payout(
            chain_client=client_wrong_amount, proof=proof, approval=approval
        )

    client_wrong_signer = MockChainClient(
        extrinsic=MockExtrinsic(
            call_module="Balances",
            call_function="transfer_keep_alive",
            call_args={"dest": alice.ss58_address, "value": 5_000_000_000},
            signer_address=eve.ss58_address,
        )
    )
    with pytest.raises(InvalidPayoutProofError):
        await verifier.verify_payout(
            chain_client=client_wrong_signer, proof=proof, approval=approval
        )


@pytest.mark.asyncio
async def test_double_payment_and_replay_prevention() -> None:
    """Ensure extrinsics and deliverable commits cannot be credited multiple times."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    treasury = bittensor.Keypair.create_from_uri("//Charlie")
    op = bittensor.Keypair.create_from_uri("//Bob")
    now = datetime.now(UTC)

    base_data: dict[str, Any] = {
        "repo": "ditto-assistant/ditto-subnet",
        "issue": 2046,
        "claim_id": _CLAIM_ID,
        "commit_sha": _COMMIT_SHA,
        "claimant_hotkey": alice.ss58_address,
        "payee_coldkey": alice.ss58_address,
        "reward_revision": 1,
        "amount_rao": 5_000_000_000,
        "amount_tao": 5.0,
        "treasury_policy_revision": 1,
        "split_shares": [],
        "approvals": [
            OperatorApproval(
                operator_hotkey=op.ss58_address,
                signature=_SIG_HEX,
                role="approver",
                approved_at=now,
            )
        ],
        "confirmation_phrase": CONFIRMATION_PHRASE,
    }
    checksum = compute_approval_checksum(base_data)
    approval = BountyApprovalRecord(**base_data, checksum=checksum, approved_at=now)

    mock_extrinsic = MockExtrinsic(
        call_module="Balances",
        call_function="transfer_keep_alive",
        call_args={"dest": alice.ss58_address, "value": 5_000_000_000},
        signer_address=treasury.ss58_address,
    )
    client = MockChainClient(
        canonical_block_hash=_BLOCK_HASH,
        extrinsic=mock_extrinsic,
        is_success=True,
    )
    verifier = BountyChainPayoutVerifier(
        authorized_treasury_coldkeys={treasury.ss58_address}
    )
    proof = BountyPayoutProof(
        block_number=100,
        block_hash=_BLOCK_HASH,
        extrinsic_index=2,
        claim_id=_CLAIM_ID,
        payee_coldkey=alice.ss58_address,
        amount_rao=5_000_000_000,
    )

    await verifier.verify_payout(chain_client=client, proof=proof, approval=approval)

    with pytest.raises(DuplicatePaymentError):
        await verifier.verify_payout(
            chain_client=client, proof=proof, approval=approval
        )

    different_claim = UUID("22222222-3333-4444-5555-666666666666")
    proof2 = BountyPayoutProof(
        block_number=101,
        block_hash="f" * 64,
        extrinsic_index=5,
        claim_id=different_claim,
        payee_coldkey=alice.ss58_address,
        amount_rao=5_000_000_000,
    )
    client2 = MockChainClient(
        canonical_block_hash="f" * 64,
        extrinsic=mock_extrinsic,
        is_success=True,
    )
    with pytest.raises(DuplicatePaymentError):
        await verifier.verify_payout(
            chain_client=client2, proof=proof2, approval=approval
        )


def test_tamper_evident_ledger_chain() -> None:
    """Test append-only hash chaining and verification across ledger entries."""
    ledger = BountyLedger()
    assert ledger.head_hash == GENESIS_HASH

    e1 = ledger.append(
        claim_id=_CLAIM_ID,
        repo="ditto-assistant/ditto-subnet",
        issue=2046,
        event="claimed",
        payload={"claimant": "5Alice"},
    )
    assert e1.prev_hash == GENESIS_HASH
    assert ledger.head_hash == e1.entry_hash

    e2 = ledger.append(
        claim_id=_CLAIM_ID,
        repo="ditto-assistant/ditto-subnet",
        issue=2046,
        event="submitted",
        payload={"commit_sha": _COMMIT_SHA},
    )
    assert e2.prev_hash == e1.entry_hash
    assert ledger.head_hash == e2.entry_hash

    e3 = ledger.append(
        claim_id=_CLAIM_ID,
        repo="ditto-assistant/ditto-subnet",
        issue=2046,
        event="approved",
        payload={"amount_rao": 5_000_000_000},
    )
    assert e3.prev_hash == e2.entry_hash
    assert ledger.verify() is True

    corrupted_entries = [
        e1,
        e2.model_copy(update={"payload": {"commit_sha": "tampered"}}),
        e3,
    ]
    assert verify_bounty_ledger_chain(corrupted_entries) is False


def test_public_accounting_projection() -> None:
    """Test public audit projections include receipts without leaking secrets."""
    alice = bittensor.Keypair.create_from_uri("//Alice")
    op = bittensor.Keypair.create_from_uri("//Bob")
    now = datetime.now(UTC)

    base_data: dict[str, Any] = {
        "repo": "ditto-assistant/ditto-subnet",
        "issue": 2046,
        "claim_id": _CLAIM_ID,
        "commit_sha": _COMMIT_SHA,
        "claimant_hotkey": alice.ss58_address,
        "payee_coldkey": alice.ss58_address,
        "reward_revision": 1,
        "amount_rao": 5_000_000_000,
        "amount_tao": 5.0,
        "treasury_policy_revision": 1,
        "split_shares": [],
        "approvals": [
            OperatorApproval(
                operator_hotkey=op.ss58_address,
                signature=_SIG_HEX,
                role="approver",
                approved_at=now,
            )
        ],
        "confirmation_phrase": CONFIRMATION_PHRASE,
    }
    checksum = compute_approval_checksum(base_data)
    approval = BountyApprovalRecord(**base_data, checksum=checksum, approved_at=now)

    sm = BountyStateMachine("claimed")
    sm.transition_to("submitted")
    sm.transition_to("accepted")
    sm.transition_to("merged")
    sm.transition_to("deployed_verified")
    sm.transition_to("approved")
    sm.transition_to("paid")

    receipt = export_public_audit_record(
        approval=approval,
        state_machine=sm,
        now=now,
    )
    assert receipt.repo == "ditto-assistant/ditto-subnet"
    assert receipt.issue == 2046
    assert receipt.status == "paid"
    assert receipt.commit_sha == _COMMIT_SHA
    assert receipt.amount_rao == 5_000_000_000
    assert len(receipt.state_transitions) == 7
