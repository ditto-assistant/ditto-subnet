"""Comprehensive verification suite for SN118 bounty operations (Epic #2054)."""

from __future__ import annotations

import json
import subprocess
import sys
from uuid import uuid4

import pytest

from ditto.bounty import (
    RAO_PER_TAO,
    InvalidActivationStageError,
    MergeCannotTriggerPaymentError,
    MultiReviewerRequiredError,
    ReceiptVerificationError,
    RehearsalLedger,
    SettlementReceipt,
    TreasuryAllocationConfig,
    TreasuryBudgetExceededError,
    TreasuryManager,
    TreasuryPausedError,
    TreasuryUnfundedError,
    create_settlement_receipt,
    run_activation_order_rehearsal,
    validate_lifecycle_transition,
    verify_receipt_reconciliation,
)


class TestActivationOrderLifecycle:
    """Validates the sequential 5-stage activation order and prerequisites."""

    def test_sequential_stage_progression(self) -> None:
        """Verify that activation stages must advance sequentially."""
        treasury = TreasuryManager()
        assert treasury.activation_stage == 1
        assert not treasury.is_funded

        treasury.set_activation_stage(2)
        assert treasury.activation_stage == 2
        assert not treasury.is_funded

        treasury.set_activation_stage(3)
        assert treasury.activation_stage == 3
        assert not treasury.is_funded

        treasury.set_activation_stage(4)
        assert treasury.activation_stage == 4
        assert treasury.is_funded

        treasury.set_activation_stage(5)
        assert treasury.activation_stage == 5
        assert treasury.is_funded

    def test_stage_skipping_prohibited(self) -> None:
        """Verify that skipping stages raises InvalidActivationStageError."""
        treasury = TreasuryManager()
        with pytest.raises(InvalidActivationStageError):
            treasury.set_activation_stage(3)

        with pytest.raises(InvalidActivationStageError):
            treasury.activate_capped_treasury(10.0)

    def test_invalid_stage_bounds(self) -> None:
        """Verify stage numbers outside 1-5 are rejected."""
        treasury = TreasuryManager()
        with pytest.raises(InvalidActivationStageError):
            treasury.set_activation_stage(0)
        with pytest.raises(InvalidActivationStageError):
            treasury.set_activation_stage(6)


class TestStage1GovernanceAndTreasuryPolicy:
    """Validates Stage 1 governance parameters, funding source, and budget ceilings."""

    def test_default_governance_configuration(self) -> None:
        """Verify default configuration aligns with governance contract."""
        config = TreasuryAllocationConfig()
        assert config.treasury_share_pct == 0.05
        assert config.funding_source == "miner_vector_cut"
        assert config.epoch_cap_tao == 10.0
        assert config.epoch_cap_rao == 10 * RAO_PER_TAO
        assert config.max_single_payout_tao == 25.0
        assert config.max_single_payout_rao == 25 * RAO_PER_TAO
        assert config.multi_reviewer_threshold_tao == 5.0
        assert config.multi_reviewer_threshold_rao == 5 * RAO_PER_TAO
        assert config.min_reviewers_for_threshold == 2

    def test_unfunded_state_rejects_live_payouts(self) -> None:
        """Verify unactivated treasury rejects live payout requests."""
        treasury = TreasuryManager()
        with pytest.raises(TreasuryUnfundedError):
            treasury.validate_payout_request(
                amount_rao=1_000_000_000,
                reviewer_count=1,
                is_rehearsal=False,
            )


class TestStage3ZeroFundRehearsalAndInvariants:
    """Validates Stage 3 board and ledger rehearsal without moving funds."""

    def test_full_activation_rehearsal_run(self) -> None:
        """Execute end-to-end rehearsal and verify report contents."""
        report = run_activation_order_rehearsal(
            repo="ditto-assistant/ditto-subnet",
            issue=2054,
            commit_sha="ef1518b0c61947b1928374829102837465819283",
            amount_rao=5_000_000_000,
        )
        assert report.rehearsal_passed
        assert report.stages_completed == [1, 2, 3, 4, 5]
        assert report.ledger_entries_count == 8
        assert len(report.ledger_head_hash) == 64
        assert report.amount_tao == 5.0
        assert isinstance(report.settlement_receipt, SettlementReceipt)
        assert report.settlement_receipt.call_module == "Balances"
        assert report.settlement_receipt.call_function == "transfer_keep_alive"

        markdown_text = report.to_markdown()
        assert "SN118 Bounty Operations - Activation Rehearsal Report" in markdown_text
        assert "ditto-assistant/ditto-subnet#2054" in markdown_text
        assert "ef1518b0c61947b1928374829102837465819283" in markdown_text

        dict_data = report.to_dict()
        assert dict_data["rehearsal_passed"] is True
        assert len(dict_data["stages_completed"]) == 5

    def test_merge_alone_cannot_trigger_payment(self) -> None:
        """Verify the invariant that code merge cannot transition to payment."""
        with pytest.raises(MergeCannotTriggerPaymentError):
            validate_lifecycle_transition("merged", "paid")

    def test_tamper_evident_ledger_chain_verification(self) -> None:
        """Verify that modifying any ledger entry breaks cryptographic chaining."""
        ledger = RehearsalLedger()
        claim_id = uuid4()

        entry1 = ledger.append(
            claim_id=claim_id,
            repo="ditto-assistant/ditto-subnet",
            issue=2054,
            event="bounty_claimed",
            payload={"action": "claim"},
        )
        entry2 = ledger.append(
            claim_id=claim_id,
            repo="ditto-assistant/ditto-subnet",
            issue=2054,
            event="bounty_submitted",
            payload={"action": "submit"},
        )
        assert entry2.prev_hash == entry1.entry_hash
        assert ledger.verify_integrity()

        object.__setattr__(entry1, "event", "tampered_event")
        assert not ledger.verify_integrity()


class TestStage4CappedTreasuryAllocation:
    """Validates Stage 4 reversible caps, rate limiting, and multi-operator controls."""

    def test_epoch_cap_enforcement(self) -> None:
        """Verify payouts exceeding the epoch cap are rejected."""
        treasury = TreasuryManager(TreasuryAllocationConfig(epoch_cap_tao=10.0))
        treasury.set_activation_stage(2)
        treasury.set_activation_stage(3)
        treasury.activate_capped_treasury()

        treasury.validate_payout_request(
            amount_rao=5 * RAO_PER_TAO,
            reviewer_count=2,
            is_rehearsal=False,
        )
        treasury.record_payout(
            claim_id=uuid4(),
            amount_rao=5 * RAO_PER_TAO,
            payee_coldkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            extrinsic_hash="0x123",
            is_rehearsal=False,
        )

        with pytest.raises(TreasuryBudgetExceededError):
            treasury.validate_payout_request(
                amount_rao=6 * RAO_PER_TAO,
                reviewer_count=2,
                is_rehearsal=False,
            )

        treasury.reset_epoch()
        treasury.validate_payout_request(
            amount_rao=6 * RAO_PER_TAO,
            reviewer_count=2,
            is_rehearsal=False,
        )

    def test_single_payout_cap_enforcement(self) -> None:
        """Verify payouts exceeding the single bounty cap are rejected."""
        treasury = TreasuryManager(TreasuryAllocationConfig(max_single_payout_tao=20.0))
        treasury.set_activation_stage(2)
        treasury.set_activation_stage(3)
        treasury.activate_capped_treasury()

        with pytest.raises(TreasuryBudgetExceededError):
            treasury.validate_payout_request(
                amount_rao=21 * RAO_PER_TAO,
                reviewer_count=2,
                is_rehearsal=False,
            )

    def test_multi_reviewer_threshold_enforcement(self) -> None:
        """Verify payouts exceeding threshold require multiple reviewers."""
        treasury = TreasuryManager(
            TreasuryAllocationConfig(multi_reviewer_threshold_tao=5.0)
        )
        treasury.set_activation_stage(2)
        treasury.set_activation_stage(3)
        treasury.activate_capped_treasury()

        with pytest.raises(MultiReviewerRequiredError):
            treasury.validate_payout_request(
                amount_rao=5 * RAO_PER_TAO,
                reviewer_count=1,
                is_rehearsal=False,
            )

        treasury.validate_payout_request(
            amount_rao=5 * RAO_PER_TAO,
            reviewer_count=2,
            is_rehearsal=False,
        )

    def test_emergency_pause_halts_all_operations(self) -> None:
        """Verify emergency pause blocks fund reservations and payout approvals."""
        treasury = TreasuryManager()
        treasury.set_activation_stage(2)
        treasury.set_activation_stage(3)
        treasury.activate_capped_treasury()

        treasury.emergency_pause(
            reason="Investigating reconciliation discrepancy",
            operator_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
        )
        assert treasury.emergency_paused

        claim_id = uuid4()
        with pytest.raises(TreasuryPausedError):
            treasury.reserve_funds(claim_id, 1 * RAO_PER_TAO)

        with pytest.raises(TreasuryPausedError):
            treasury.validate_payout_request(1 * RAO_PER_TAO, 1)

        treasury.resume(
            operator_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
        )
        assert not treasury.emergency_paused
        reservation = treasury.reserve_funds(claim_id, 1 * RAO_PER_TAO)
        assert reservation.active


class TestStage5PublicReceiptReconciliation:
    """Validates Stage 5 on-chain reconciliation and receipt generation."""

    def test_receipt_reconciliation_validation(self) -> None:
        """Verify on-chain reconciliation checks detect parameter mismatches."""
        payee = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
        block_hash = "f" * 64
        commit_sha = "a" * 40

        verify_receipt_reconciliation(
            expected_payee=payee,
            actual_payee=payee,
            expected_amount_rao=1000,
            actual_amount_rao=1000,
            call_module="Balances",
            call_function="transfer_keep_alive",
            block_hash=block_hash,
            commit_sha=commit_sha,
        )

        with pytest.raises(ReceiptVerificationError):
            verify_receipt_reconciliation(
                expected_payee=payee,
                actual_payee="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
                expected_amount_rao=1000,
                actual_amount_rao=1000,
                call_module="Balances",
                call_function="transfer_keep_alive",
                block_hash=block_hash,
                commit_sha=commit_sha,
            )

        with pytest.raises(ReceiptVerificationError):
            verify_receipt_reconciliation(
                expected_payee=payee,
                actual_payee=payee,
                expected_amount_rao=1000,
                actual_amount_rao=2000,
                call_module="Balances",
                call_function="transfer_keep_alive",
                block_hash=block_hash,
                commit_sha=commit_sha,
            )

        with pytest.raises(ReceiptVerificationError):
            verify_receipt_reconciliation(
                expected_payee=payee,
                actual_payee=payee,
                expected_amount_rao=1000,
                actual_amount_rao=1000,
                call_module="SubtensorModule",
                call_function="transfer_keep_alive",
                block_hash=block_hash,
                commit_sha=commit_sha,
            )

    def test_create_settlement_receipt(self) -> None:
        """Verify settlement receipt generation and formatting."""
        receipt = create_settlement_receipt(
            claim_id=uuid4(),
            repo="ditto-assistant/ditto-subnet",
            issue=2054,
            commit_sha="a" * 40,
            claimant_hotkey="5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty",
            payee_coldkey="5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY",
            amount_rao=5_000_000_000,
            treasury_coldkey="5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy",
            block_hash="b" * 64,
            extrinsic_index=1,
            ledger_entry_hash="c" * 64,
        )
        assert receipt.amount_tao == 5.0
        json_str = receipt.to_json()
        assert "5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy" in json_str

        md_str = receipt.to_markdown()
        assert "SN118 Bounty Settlement Receipt" in md_str
        assert "5.000000 TAO" in md_str


class TestRehearsalCLI:
    """Validates the CLI execution of the rehearsal tool."""

    def test_cli_execution_json_output(self) -> None:
        """Verify rehearsal CLI runs cleanly and emits valid JSON."""
        result = subprocess.run(
            [sys.executable, "scripts/rehearse_bounty_ledger.py", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, f"CLI failed: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["rehearsal_passed"] is True
        assert len(data["stages_completed"]) == 5
        assert data["amount_tao"] == 5.0
