from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ditto.treasury.gm import CreditBalance
from ditto.treasury.schedule import DailyPolicy, create_daily_request

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)


def _policy() -> DailyPolicy:
    return DailyPolicy(
        gm_account_ref="omniaura-gm",
        route="tao",
        floor_nano_usd=2_000_000_000,
        target_nano_usd=5_000_000_000,
        source_alpha_rao=1_000_000_000,
        max_source_alpha_rao=2_000_000_000,
    )


def test_daily_request_is_idempotent_and_does_not_spend(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    assert (
        create_daily_request(
            outbox, _policy(), CreditBalance(3_000_000_000, "3.000000000"), now=NOW
        )
        is None
    )
    path = create_daily_request(
        outbox, _policy(), CreditBalance(1_000_000_000, "1.000000000"), now=NOW
    )
    assert path is not None
    body = json.loads(path.read_text())
    assert body["state"] == "needs_current_billing_instructions_and_quote"
    assert body["source_alpha_rao"] == 1_000_000_000
    assert (
        create_daily_request(
            outbox, _policy(), CreditBalance(0, "0.000000000"), now=NOW
        )
        == path
    )
    assert len(list(outbox.glob("*.json"))) == 1


def test_daily_source_cap(tmp_path: Path) -> None:
    policy = DailyPolicy("account", "tao", 1, 2, 11_000_000_000, 11_000_000_000)
    with pytest.raises(ValueError, match="ceiling"):
        create_daily_request(tmp_path, policy, CreditBalance(0, "0.000000000"), now=NOW)
