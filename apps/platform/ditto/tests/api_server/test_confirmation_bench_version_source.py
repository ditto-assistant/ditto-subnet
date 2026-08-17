"""The confirmation lane must read its benchmark rule from exactly one place.

This lane was stranded on bench 9 while the network ran on 11 because the rule
"which benchmarks can be confirmed" was restated as a bare literal in eight
different modules -- policy, persistence, reconciliation, the score-finalization
trigger, and four SQL branches. Each was individually reasonable and the set was
collectively wrong, and nothing failed loudly when the epoch advanced.

The rule now has one definition (``supports_confirmation``, derived from the
``V9EvidenceBenchVersion`` alias). These tests keep it that way.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from ditto_screening_protocol.bench_v9 import (
    CONFIRMATION_BENCH_VERSIONS,
    MIN_CONFIRMATION_BENCH_VERSION,
    V9EvidenceBenchVersion,
    supports_confirmation,
)

_PLATFORM_ROOT = Path(__file__).resolve().parents[2]

# Every module that gates, ranks, or persists confirmation work.
_CONFIRMATION_LANE = (
    "api_server/confirmation_bundles.py",
    "api_server/confirmation_candidate_reconciliation.py",
    "db/queries/confirmation_bundles.py",
)

# A bare comparison of a bench version against a literal. This is the shape that
# caused the outage; the lane must express the rule through the shared helper.
_BARE_COMPARISON = re.compile(
    r"bench_version\s*(==|!=|>=|<=|<|>)\s*\d+"
    r"|bench_version\s*=\s*\d+",
)


def test_supported_versions_are_derived_not_restated() -> None:
    """The version set is the evidence alias, not a parallel constant."""
    assert get_args(V9EvidenceBenchVersion) == CONFIRMATION_BENCH_VERSIONS
    assert min(CONFIRMATION_BENCH_VERSIONS) == MIN_CONFIRMATION_BENCH_VERSION


@pytest.mark.parametrize("version", get_args(V9EvidenceBenchVersion))
def test_every_evidence_epoch_can_be_confirmed(version: int) -> None:
    """Carrying the evidence contract forward is the whole admission rule."""
    assert supports_confirmation(version) is True


@pytest.mark.parametrize("version", [None, 0, 1, 8])
def test_pre_contract_epochs_are_refused(version: int | None) -> None:
    assert supports_confirmation(version) is False


def test_an_unextended_future_epoch_fails_closed() -> None:
    """Activating an epoch without carrying evidence forward creates no work.

    Membership rather than ``>= MIN`` on purpose: a bundle whose base proof can
    never be parsed is worse than no bundle, because it consumes the daily cap
    and reads as a failing lane.
    """
    assert supports_confirmation(max(CONFIRMATION_BENCH_VERSIONS) + 1) is False


@pytest.mark.parametrize("relative_path", _CONFIRMATION_LANE)
def test_lane_never_restates_the_version_rule(relative_path: str) -> None:
    """No module in the lane compares a bench version to a literal.

    If this fails, the fix is to call ``supports_confirmation`` (or
    ``confirmation_capable`` for a SQL branch) rather than to widen the literal:
    a second definition is exactly how the lane fell behind the network.
    """
    source = (_PLATFORM_ROOT / relative_path).read_text()
    offenders = sorted(
        {line.strip() for line in source.splitlines() if _BARE_COMPARISON.search(line)}
    )
    assert not offenders, (
        f"{relative_path} restates the confirmation benchmark rule: {offenders}"
    )
