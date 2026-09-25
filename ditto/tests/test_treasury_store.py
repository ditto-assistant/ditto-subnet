from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ditto.treasury import execution
from ditto.treasury.preflight import TopUpBounds, TopUpIntent
from ditto.treasury.store import PaymentPlan, TreasuryStore

NOW = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
HASH = "0x" + "a" * 64


def _instructions(route: str) -> bytes:
    return json.dumps(
        {
            "asset": route,
            "source": "reviewed-wallet",
            "destination": "destination-wallet",
            "hotkey": "gm-hotkey" if route == "gm_alpha" else "",
            "account_ref": "reviewed-gm-account",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


INSTRUCTIONS = hashlib.sha256(_instructions("tao")).hexdigest()


def _plan(key: str = "payment-0001", *, route: str = "tao") -> PaymentPlan:
    return PaymentPlan(
        intent=TopUpIntent(
            route=route,
            policy_revision=1,
            quote_block_hash=HASH,
            quoted_at=NOW,
            source_alpha_rao=1_000_000_000,
            tao_value_rao=7_000_000,
            price_impact_bps=1,
            linked_wallet="reviewed-wallet",
            payment_instructions_sha256=hashlib.sha256(
                _instructions(route)
            ).hexdigest(),
            idempotency_key=key,
        ),
        destination_coldkey="destination-wallet",
        treasury_hotkey="treasury-hotkey",
        gm_hotkey="gm-hotkey" if route == "gm_alpha" else "",
        gm_account_ref="reviewed-gm-account",
        operator="proposer",
        instructions_observed_at=NOW,
        gm_balance_before_nano_usd=1_000_000_000,
        min_tao_proceeds_rao=6_500_000,
        min_gm_alpha_rao=300_000_000 if route == "gm_alpha" else 0,
        max_slippage_bps=50,
    )


def _bounds(route: str = "tao") -> TopUpBounds:
    return TopUpBounds(
        max_source_alpha_rao=2_000_000_000,
        max_single_topup_rao=10_000_000,
        max_daily_outflow_rao=10_000_000,
        max_slippage_bps=50,
        daily_spent_rao=0,
        expected_linked_wallet="reviewed-wallet",
        expected_payment_instructions_sha256=hashlib.sha256(
            _instructions(route)
        ).hexdigest(),
        reconciled=True,
    )


def _fund(store: TreasuryStore) -> None:
    store.record_allocation(
        epoch=100,
        policy_revision=1,
        source_block_hash=HASH,
        gm_alpha_rao=2_000_000_000,
        maintenance_alpha_rao=1_000_000_000,
        operator="allocator",
        reviewer="allocation-reviewer",
        now=NOW,
    )


def test_gm_cannot_spend_maintenance_or_unallocated_alpha(tmp_path: Path) -> None:
    store = TreasuryStore(tmp_path / "treasury.db")
    store.set_pause(
        False,
        operator="admin",
        reviewer="reviewer",
        reason="reviewed treasury test",
        now=NOW,
    )
    with pytest.raises(ValueError, match="GM budget"):
        store.create_plan(_plan(), _bounds(), now=NOW)
    store.record_allocation(
        epoch=100,
        policy_revision=1,
        source_block_hash=HASH,
        gm_alpha_rao=100_000_000,
        maintenance_alpha_rao=2_000_000_000,
        operator="allocator",
        reviewer="allocation-reviewer",
        now=NOW,
    )
    with pytest.raises(ValueError, match="GM budget"):
        store.create_plan(_plan(), _bounds(), now=NOW)


def test_tao_payment_requires_reviewer_and_reconciles(tmp_path: Path) -> None:
    store = TreasuryStore(tmp_path / "treasury.db")
    _fund(store)
    with pytest.raises(ValueError, match="paused"):
        store.create_plan(_plan(), _bounds(), now=NOW)
    store.set_pause(
        False,
        operator="admin",
        reviewer="reviewer",
        reason="reviewed treasury test",
        now=NOW,
    )
    plan_hash = store.create_plan(_plan(), _bounds(), now=NOW)
    assert store.create_plan(_plan(), _bounds(), now=NOW) == plan_hash
    with pytest.raises(ValueError, match="independent"):
        store.approve(
            "payment-0001", reviewer="proposer", expected_plan_hash=plan_hash, now=NOW
        )
    store.approve(
        "payment-0001", reviewer="reviewer", expected_plan_hash=plan_hash, now=NOW
    )
    leg = store.claim_leg("payment-0001", now=NOW)
    assert leg["leg"] == "unstake"
    with pytest.raises(ValueError, match="not approved"):
        store.claim_leg("payment-0001", now=NOW)
    with pytest.raises(ValueError, match="unresolved"):
        store.set_pause(
            False,
            operator="admin",
            reviewer="reviewer",
            reason="ignore unresolved attempt",
            now=NOW,
        )
    store.finalize_leg(
        "payment-0001",
        leg="unstake",
        extrinsic_hash=HASH,
        block_hash=HASH,
        next_amount_rao=6_900_000,
        now=NOW,
    )
    store.approve_next_leg(
        "payment-0001",
        reviewer="reviewer",
        expected_plan_hash=plan_hash,
        instructions_sha256=INSTRUCTIONS,
        quote_block_hash=HASH,
        quote_observed_at=NOW,
        now=NOW,
    )
    assert store.claim_leg("payment-0001", now=NOW)["leg"] == "deposit_tao"
    store.finalize_leg(
        "payment-0001",
        leg="deposit_tao",
        extrinsic_hash=HASH,
        block_hash=HASH,
        next_amount_rao=None,
        now=NOW,
    )
    store.reconcile_gm(
        "payment-0001",
        before_nano_usd=1_000_000_000,
        after_nano_usd=1_500_000_000,
        deposit_nano_usd=600_000_000,
        intervening_usage_nano_usd=100_000_000,
        deposit_reference="GM deposit 123",
        reviewer="reviewer",
        now=NOW,
    )
    assert store.status()["plans"][0]["state"] == "reconciled"
    assert store.verify() == 10


def test_gm_alpha_route_and_daily_cap(tmp_path: Path) -> None:
    store = TreasuryStore(tmp_path / "treasury.db")
    _fund(store)
    store.set_pause(
        False,
        operator="admin",
        reviewer="reviewer",
        reason="reviewed treasury test",
        now=NOW,
    )
    plan_hash = store.create_plan(_plan(route="gm_alpha"), _bounds("gm_alpha"), now=NOW)
    store.approve(
        "payment-0001", reviewer="reviewer", expected_plan_hash=plan_hash, now=NOW
    )
    assert store.claim_leg("payment-0001", now=NOW)["leg"] == "unstake"
    store.finalize_leg(
        "payment-0001",
        leg="unstake",
        extrinsic_hash=HASH,
        block_hash=HASH,
        next_amount_rao=6_900_000,
        now=NOW,
    )
    store.approve_next_leg(
        "payment-0001",
        reviewer="reviewer",
        expected_plan_hash=plan_hash,
        instructions_sha256=hashlib.sha256(_instructions("gm_alpha")).hexdigest(),
        quote_block_hash=HASH,
        quote_observed_at=NOW,
        now=NOW,
    )
    assert store.claim_leg("payment-0001", now=NOW)["leg"] == "stake_gm"
    store.finalize_leg(
        "payment-0001",
        leg="stake_gm",
        extrinsic_hash=HASH,
        block_hash=HASH,
        next_amount_rao=320_000_000,
        now=NOW,
    )
    store.approve_next_leg(
        "payment-0001",
        reviewer="reviewer",
        expected_plan_hash=plan_hash,
        instructions_sha256=hashlib.sha256(_instructions("gm_alpha")).hexdigest(),
        quote_block_hash=HASH,
        quote_observed_at=NOW,
        now=NOW,
    )
    assert store.claim_leg("payment-0001", now=NOW)["leg"] == "deposit_gm"
    store.finalize_leg(
        "payment-0001",
        leg="deposit_gm",
        extrinsic_hash=HASH,
        block_hash=HASH,
        next_amount_rao=None,
        now=NOW,
    )
    store.reconcile_gm(
        "payment-0001",
        before_nano_usd=1_000_000_000,
        after_nano_usd=1_500_000_000,
        deposit_nano_usd=600_000_000,
        intervening_usage_nano_usd=100_000_000,
        deposit_reference="GM deposit 123",
        reviewer="reviewer",
        now=NOW,
    )
    with pytest.raises(ValueError, match="daily outflow"):
        store.create_plan(_plan("payment-0002"), _bounds(), now=NOW)


def test_failed_dispatch_is_paused_and_never_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TreasuryStore(tmp_path / "treasury.db")
    _fund(store)
    store.set_pause(
        False,
        operator="admin",
        reviewer="reviewer",
        reason="reviewed treasury test",
        now=NOW,
    )
    plan_hash = store.create_plan(_plan(), _bounds(), now=NOW)
    store.approve(
        "payment-0001", reviewer="reviewer", expected_plan_hash=plan_hash, now=NOW
    )

    class _Key:
        ss58_address = "reviewed-wallet"

    monkeypatch.setattr(
        execution,
        "_load_wallet",
        lambda _project: type("W", (), {"coldkeypub": _Key()})(),
    )
    calls = 0

    def fail_dispatch(*_args: object) -> None:
        nonlocal calls
        calls += 1
        raise TimeoutError("ambiguous chain response")

    monkeypatch.setattr(execution, "dispatch_chain_leg", fail_dispatch)
    with pytest.raises(ValueError, match="instruction fields"):
        execution.execute_one_leg(
            store, "payment-0001", project="project", instructions=b"{}", now=NOW
        )
    assert store.status()["plans"][0]["state"] == "approved"
    with pytest.raises(TimeoutError):
        execution.execute_one_leg(
            store,
            "payment-0001",
            project="project",
            instructions=_instructions("tao"),
            now=NOW,
        )
    assert calls == 1
    assert store.status()["paused"] is True
    assert store.status()["plans"][0]["state"] == "dispatching"
    with pytest.raises(ValueError, match="paused"):
        execution.execute_one_leg(
            store,
            "payment-0001",
            project="project",
            instructions=_instructions("tao"),
            now=NOW,
        )
    assert calls == 1
