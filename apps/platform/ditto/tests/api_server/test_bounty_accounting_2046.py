"""Automated tests for bounty acceptance, alpha awards, fee refunds and accounting (#2046)."""

from __future__ import annotations

import importlib.util
import os
import sys
from uuid import uuid4

import pytest

MOD_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "api_server",
    "bounty_accounting.py",
)
spec = importlib.util.spec_from_file_location("bounty_accounting", MOD_PATH)
mod = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)

BountyAccountingLedger = mod.BountyAccountingLedger

FEE = 2_000_000_000
POOL = 100_000_000_000


def test_single_winner_award_and_fee_refund_and_rejected_expired_forfeit():
    ledger = BountyAccountingLedger(bounty_id=uuid4(), reward_pool_alpha_rao=POOL)

    # 3 attempts in Round 1: Alice (accepted), Bob (rejected), Carol (expired)
    alice = ledger.register_paid_attempt(
        hotkey="5AliceHotkey",
        payee_coldkey="5AliceColdkey",
        github_login="alice",
        entry_fee_alpha_rao=FEE,
        fee_extrinsic_ref="0xaaa1#1",
    )
    bob = ledger.register_paid_attempt(
        hotkey="5BobHotkey",
        payee_coldkey="5BobColdkey",
        github_login="bob",
        entry_fee_alpha_rao=FEE,
        fee_extrinsic_ref="0xbbb2#1",
    )
    carol = ledger.register_paid_attempt(
        hotkey="5CarolHotkey",
        payee_coldkey="5CarolColdkey",
        github_login="carol",
        entry_fee_alpha_rao=FEE,
        fee_extrinsic_ref="0xccc3#1",
    )

    ledger.submit_attempt(
        alice.claim_id,
        pr_number=3001,
        head_sha="a" * 40,
        validation_output="12 passed in 0.42s",
    )
    ledger.submit_attempt(
        bob.claim_id,
        pr_number=3002,
        head_sha="b" * 40,
        validation_output="3 failed, 9 passed",
    )

    # Reviewer requests changes first on Alice, then Alice resubmits and is accepted
    ledger.record_decision(
        alice.claim_id,
        decision="changes_requested",
        reviewer_actor="reviewer@ditto.ai",
        rationale="Add edge-case test for duplicate payout",
        rework_days=3,
    )
    assert alice.status == "rework"

    ledger.submit_attempt(
        alice.claim_id,
        pr_number=3001,
        head_sha="c" * 40,
        validation_output="14 passed in 0.45s",
    )
    ledger.record_decision(
        alice.claim_id,
        decision="accepted",
        reviewer_actor="reviewer@ditto.ai",
        rationale="All acceptance evidence verified",
        attribution="Implemented by @alice",
        operator_approvals=["approver1@ditto.ai"],
    )

    # Bob is rejected, Carol misses deadline (expired)
    ledger.record_decision(
        bob.claim_id,
        decision="rejected",
        reviewer_actor="reviewer@ditto.ai",
        rationale="Fails required unit tests",
    )
    ledger.mark_expired_or_withdrawn(
        carol.claim_id,
        outcome="expired",
        rationale="5-day delivery deadline elapsed without submission",
    )

    # Single-winner award to Alice
    award_entry = ledger.award_single_winner(
        alice.claim_id,
        amount_alpha_rao=POOL,
        decider_actor="lead@ditto.ai",
        operator_approvals=["approver1@ditto.ai", "approver2@ditto.ai"],
        rationale="Full pool awarded to sole accepted attempt",
    )
    assert award_entry.amount_alpha_rao == POOL
    assert award_entry.reason == "bounty_award"

    # Duplicate payout is prevented
    with pytest.raises(ValueError, match="duplicate payout prevented"):
        ledger.award_single_winner(
            alice.claim_id,
            amount_alpha_rao=1_000,
            decider_actor="lead@ditto.ai",
            operator_approvals=["approver1@ditto.ai"],
            rationale="Second payout attempt",
        )

    summary = ledger.summary()
    assert summary.total_fees_collected_rao == 3 * FEE
    assert summary.fees_refunded_rao == FEE
    assert summary.fees_retained_rao == 2 * FEE
    assert summary.alpha_awarded_rao == POOL
    assert summary.escrow_balance_rao == 2 * FEE


def test_shared_winner_split_and_operator_recusal():
    ledger = BountyAccountingLedger(bounty_id=uuid4(), reward_pool_alpha_rao=POOL)

    a1 = ledger.register_paid_attempt(
        hotkey="5Hot1",
        payee_coldkey="5Cold1",
        github_login="dev1",
        entry_fee_alpha_rao=FEE,
        fee_extrinsic_ref="0x111#0",
    )
    a2 = ledger.register_paid_attempt(
        hotkey="5Hot2",
        payee_coldkey="5Cold2",
        github_login="dev2",
        entry_fee_alpha_rao=FEE,
        fee_extrinsic_ref="0x222#0",
    )

    for att, pr, sha_ch in ((a1, 401, "1"), (a2, 402, "2")):
        ledger.submit_attempt(
            att.claim_id,
            pr_number=pr,
            head_sha=sha_ch * 40,
            validation_output="all tests green",
        )
        ledger.record_decision(
            att.claim_id,
            decision="accepted",
            reviewer_actor="reviewer@ditto.ai",
            rationale="Material contribution accepted",
            operator_approvals=["treasury1@ditto.ai"],
        )

    # Reviewer or award decider cannot approve payout
    with pytest.raises(PermissionError, match="cannot approve"):
        ledger.award_shared_winners(
            awards=[
                {"claim_id": a1.claim_id, "amount_alpha_rao": 60_000_000_000},
                {"claim_id": a2.claim_id, "amount_alpha_rao": 40_000_000_000},
            ],
            decider_actor="decider@ditto.ai",
            operator_approvals=["reviewer@ditto.ai", "treasury1@ditto.ai"],
            rationale="60/40 split",
        )

    entries = ledger.award_shared_winners(
        awards=[
            {"claim_id": a1.claim_id, "amount_alpha_rao": 60_000_000_000, "attribution": "Core engine by @dev1"},
            {"claim_id": a2.claim_id, "amount_alpha_rao": 40_000_000_000, "attribution": "Test harness by @dev2"},
        ],
        decider_actor="decider@ditto.ai",
        operator_approvals=["treasury1@ditto.ai", "treasury2@ditto.ai"],
        rationale="Shared award: 60% core engine, 40% test suite",
    )
    assert len(entries) == 2

    summary = ledger.summary()
    assert summary.total_fees_collected_rao == 2 * FEE
    assert summary.fees_refunded_rao == 2 * FEE
    assert summary.fees_retained_rao == 0
    assert summary.alpha_awarded_rao == POOL
    assert summary.escrow_balance_rao == 0


def test_team_cancellation_refunds_entry_fee_in_full():
    ledger = BountyAccountingLedger(bounty_id=uuid4(), reward_pool_alpha_rao=POOL)
    attempt = ledger.register_paid_attempt(
        hotkey="5HotCancel",
        payee_coldkey="5ColdCancel",
        github_login="dev-cancel",
        entry_fee_alpha_rao=FEE,
        fee_extrinsic_ref="0x999#0",
    )
    ledger.record_decision(
        attempt.claim_id,
        decision="cancelled_by_team",
        reviewer_actor="ops@ditto.ai",
        rationale="Upstream architecture superseded this bounty",
        operator_approvals=["treasury1@ditto.ai"],
    )
    summary = ledger.summary()
    assert summary.total_fees_collected_rao == FEE
    assert summary.fees_refunded_rao == FEE
    assert summary.fees_retained_rao == 0
    assert summary.escrow_balance_rao == 0
    assert summary.entries[0].reason == "team_cancel_refund"
