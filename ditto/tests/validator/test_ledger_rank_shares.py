"""Protocol 31: the validator folds the served operator payout curve."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ditto.validator.build_info import HEARTBEAT_PROTOCOL_VERSION
from ditto.validator.config import KOTH_RANK_SHARES
from ditto.validator.worker import _ledger_rank_shares


def test_validator_advertises_protocol_31() -> None:
    assert HEARTBEAT_PROTOCOL_VERSION >= 31


def test_absent_curve_keeps_the_frozen_shares() -> None:
    assert _ledger_rank_shares(SimpleNamespace(), KOTH_RANK_SHARES) == KOTH_RANK_SHARES
    assert (
        _ledger_rank_shares(SimpleNamespace(rank_shares=None), KOTH_RANK_SHARES)
        == KOTH_RANK_SHARES
    )


def test_served_curve_replaces_the_frozen_shares() -> None:
    ledger = SimpleNamespace(rank_shares=[0.5, 0.2, 0.13, 0.1, 0.07])
    assert _ledger_rank_shares(ledger, KOTH_RANK_SHARES) == (0.5, 0.2, 0.13, 0.1, 0.07)


@pytest.mark.parametrize(
    "served",
    [
        [0.5, 0.5],
        [0.5, 0.2, 0.13, 0.1, 0.0],
        [0.5, 0.2, 0.13, 0.1, 0.08],
        [0.5, 0.2, 0.13, 0.1, float("nan")],
    ],
)
def test_unusable_curve_falls_back_identically(served: list[float]) -> None:
    ledger = SimpleNamespace(rank_shares=served)
    assert _ledger_rank_shares(ledger, KOTH_RANK_SHARES) == KOTH_RANK_SHARES
