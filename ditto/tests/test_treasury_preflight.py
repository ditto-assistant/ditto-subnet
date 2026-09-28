from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from ditto.treasury.preflight import TopUpBounds, TopUpIntent, preflight


def _case() -> tuple[TopUpIntent, TopUpBounds, datetime]:
    now = datetime.now(UTC)
    intent = TopUpIntent(
        route="tao",
        policy_revision=2,
        quote_block_hash="0x" + "a" * 64,
        quoted_at=now,
        source_alpha_rao=1_000_000_000,
        tao_value_rao=7_400_000,
        price_impact_bps=10,
        linked_wallet="reviewed-wallet",
        payment_instructions_sha256="b" * 64,
        idempotency_key="topup-1",
    )
    bounds = TopUpBounds(
        max_source_alpha_rao=2_000_000_000,
        max_single_topup_rao=10_000_000,
        max_daily_outflow_rao=20_000_000,
        max_slippage_bps=50,
        daily_spent_rao=0,
        expected_linked_wallet="reviewed-wallet",
        expected_payment_instructions_sha256="b" * 64,
        reconciled=True,
    )
    return intent, bounds, now


def test_both_routes_require_the_same_reviewed_bounds() -> None:
    intent, bounds, now = _case()
    preflight(intent, bounds, now=now)
    preflight(replace(intent, route="gm_alpha"), bounds, now=now)


@pytest.mark.parametrize(
    "change",
    [
        {"route": "other"},
        {"quoted_at": datetime.now(UTC) - timedelta(minutes=3)},
        {"linked_wallet": "unlinked"},
        {"payment_instructions_sha256": "c" * 64},
        {"tao_value_rao": 11_000_000},
        {"price_impact_bps": 51},
    ],
)
def test_refuses_unsafe_intent(change: dict) -> None:
    intent, bounds, now = _case()
    with pytest.raises(ValueError):
        preflight(replace(intent, **change), bounds, now=now)


def test_refuses_unreconciled_and_daily_limit() -> None:
    intent, bounds, now = _case()
    with pytest.raises(ValueError, match="reconciled"):
        preflight(intent, replace(bounds, reconciled=False), now=now)
    with pytest.raises(ValueError, match="daily"):
        preflight(intent, replace(bounds, daily_spent_rao=13_000_000), now=now)
