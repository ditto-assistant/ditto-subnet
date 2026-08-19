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

# Modules that *project* confirmation state -- ranking, the emission-adjacent
# KOTH entries, the public board, and the frozen profile digests. These were
# outside the sweep above, which is exactly why seven ``== 9`` literals here
# survived the v10 and v11 carry-forwards and ranked confirmed rows on their
# unconfirmed composite. They also contain deliberate era floors (``>= 9`` /
# ``< 9``) and unrelated axes, so only equality against a literal is banned,
# and each surviving exception is named below rather than tolerated by shape.
_CONFIRMATION_PROJECTION = (
    "api_server/confirmation_evidence.py",
    "api_server/confirmation_profile_installation.py",
    "api_server/endpoints/public.py",
    "api_server/endpoints/validator.py",
    "api_server/endpoints/validator_confirmation.py",
    "db/queries/score_ranking.py",
)

_EQUALITY_COMPARISON = re.compile(r"bench_version\s*(==|!=)\s*\d+")

# Documented non-confirmation uses of an exact bench-version literal. Each entry
# is (module, exact source line). Adding one is a decision, not a formality.
_PROJECTION_EXCEPTIONS = {
    (
        "api_server/endpoints/public.py",
        "if bench_version == 9 or v9_base is not None",
    ): (
        "Selects which model-use factor family a row uses. Bench 9 always "
        "carries a signed root; later epochs are covered by the "
        "'or v9_base is not None' arm, and rows scored before the evidence "
        "contract carried forward deliberately keep the legacy platform "
        "factor instead of misreporting an enforcement zero."
    ),
}

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


@pytest.mark.parametrize("relative_path", _CONFIRMATION_PROJECTION)
def test_projection_never_compares_a_bench_version_to_a_literal(
    relative_path: str,
) -> None:
    """Ranking and display must ask the shared rule, not a remembered number.

    An era floor (``>= 9``) is a different statement from "this epoch can be
    confirmed" and stays legal here; equality against a literal is the shape
    that silently strands the lane one epoch behind the network.
    """
    source = (_PLATFORM_ROOT / relative_path).read_text()
    offenders = sorted(
        {
            line.strip()
            for line in source.splitlines()
            if _EQUALITY_COMPARISON.search(line)
            and (relative_path, line.strip()) not in _PROJECTION_EXCEPTIONS
        }
    )
    assert not offenders, (
        f"{relative_path} restates the confirmation benchmark rule: {offenders}. "
        "Call supports_confirmation (or confirmation_capable for SQL); if the "
        "literal is genuinely a different axis, add it to "
        "_PROJECTION_EXCEPTIONS with the reason."
    )


def test_every_declared_projection_exception_still_exists() -> None:
    """A stale allowlist entry is a silent hole in the guard above."""
    for (relative_path, line), reason in _PROJECTION_EXCEPTIONS.items():
        source = (_PLATFORM_ROOT / relative_path).read_text()
        assert any(candidate.strip() == line for candidate in source.splitlines()), (
            f"{relative_path} no longer contains the allowlisted line {line!r} "
            f"({reason}); remove the exception."
        )
