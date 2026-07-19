"""Version-bump re-score sweep + platform-authoritative hybrid weight fold.

Benchmark scores are only comparable within one ``bench_version``: a version
bump changes what the composite means. The platform therefore selects one row
per agent (v3 at quorum, otherwise v2) and validators fold the resulting hybrid
pool without applying a second global version filter. The worker still uses
versions to schedule stale champion + tail re-evaluations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.validator.weights import (
    MIN_ELIGIBLE_CASES,
    agents_needing_rescore,
    compute_weights,
    filter_eligible,
    filter_to_latest_version,
    max_bench_version,
)
from ditto.validator.worker import ValidatorWorker

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_KOTH: dict[str, Any] = {"margin": 0.01, "tail_size": 4, "champion_share": 0.9}


def _e(
    miner: str,
    composite: float,
    *,
    version: int | None = None,
    minutes: int = 0,
    n: int | None = None,
) -> Any:
    """A duck-typed ledger entry. version=None models a platform that does not
    surface bench_version (the field is simply absent); n=None likewise omits the
    case count (the fold then treats the entry as eligible — fail open)."""
    ns = SimpleNamespace(
        miner_hotkey=miner,
        agent_id=uuid4(),
        composite=composite,
        first_seen=_T0 + timedelta(minutes=minutes),
        sha256="ab" * 32,
    )
    if version is not None:
        ns.bench_version = version
    if n is not None:
        ns.n = n
    return ns


class TestEligibilityFilter:
    def test_missing_n_is_eligible(self) -> None:
        # No case count ⇒ fail open ⇒ filter is identity (matches the version
        # filter's treatment of a missing bench_version).
        entries = [_e("a", 0.8), _e("b", 0.7)]
        assert len(filter_eligible(entries)) == 2

    def test_drops_below_floor(self) -> None:
        entries = [
            _e("full", 0.55, n=MIN_ELIGIBLE_CASES),
            _e("smoke", 0.95, n=12),
        ]
        assert {e.miner_hotkey for e in filter_eligible(entries)} == {"full"}

    def test_smoke_run_cannot_be_champion(self) -> None:
        # A high-composite smoke run must earn nothing; the lower full run wins.
        entries = [
            _e("smoke", 0.95, version=2, minutes=0, n=12),
            _e("full", 0.55, version=2, minutes=1, n=MIN_ELIGIBLE_CASES),
        ]
        w = compute_weights(entries, **_KOTH)
        assert "smoke" not in w
        assert w["full"] == pytest.approx(0.9)


class TestVersionFilter:
    def test_no_version_defaults_to_baseline(self) -> None:
        entries = [_e("a", 0.8), _e("b", 0.7)]
        assert max_bench_version(entries) == 1
        # No version info ⇒ every entry is "current" ⇒ filter is identity.
        assert len(filter_to_latest_version(entries)) == 2

    def test_filter_keeps_only_max_version(self) -> None:
        entries = [
            _e("old_champ", 0.95, version=2),
            _e("new_a", 0.60, version=3),
            _e("new_b", 0.55, version=3),
        ]
        kept = filter_to_latest_version(entries)
        assert {e.miner_hotkey for e in kept} == {"new_a", "new_b"}

    def test_fold_keeps_platform_authoritative_hybrid_pool(self) -> None:
        # Each row is already the platform's authoritative per-agent selection.
        # The v2 fallback remains until that agent reaches v3 quorum.
        entries = [
            _e("v2_fallback", 0.99, version=2, minutes=0),
            _e("v3_champ", 0.50, version=3, minutes=1),
            _e("v3_runner", 0.40, version=3, minutes=2),
        ]
        w = compute_weights(entries, **_KOTH)
        assert w["v2_fallback"] == pytest.approx(0.9)
        assert "v3_champ" in w
        assert "v3_runner" in w

    def test_additive_version_field_does_not_split_old_and_new_validators(self) -> None:
        versioned = [
            _e("fallback", 0.80, version=2, minutes=0),
            _e("complete", 0.70, version=3, minutes=1),
        ]
        legacy = [
            _e("fallback", 0.80, minutes=0),
            _e("complete", 0.70, minutes=1),
        ]
        # Old clients discard the additive field; upgraded clients retain it.
        # Both must fold the same platform-authoritative pool.
        assert compute_weights(versioned, **_KOTH) == compute_weights(legacy, **_KOTH)


class TestAgentsNeedingRescore:
    def test_selects_stale_champion_and_tail(self) -> None:
        entries = [
            _e("champ", 0.90, version=2, minutes=0),
            _e("r1", 0.70, version=2, minutes=1),
            _e("r2", 0.50, version=2, minutes=2),
        ]
        stale = agents_needing_rescore(
            entries, current_version=3, margin=0.01, tail_size=4
        )
        assert {e.miner_hotkey for e in stale} == {"champ", "r1", "r2"}

    def test_current_version_entries_not_restaged(self) -> None:
        entries = [
            _e("champ", 0.90, version=3, minutes=0),
            _e("r1", 0.70, version=3, minutes=1),
        ]
        assert (
            agents_needing_rescore(entries, current_version=3, margin=0.01, tail_size=4)
            == []
        )

    def test_empty_on_no_positive_scores(self) -> None:
        assert (
            agents_needing_rescore(
                [_e("z", 0.0, version=1)], current_version=3, margin=0.01, tail_size=4
            )
            == []
        )


def _worker() -> Any:
    """A ValidatorWorker with mocked collaborators, typed Any so the tests can
    freely stub methods/attributes without mypy method-assign noise."""
    cfg = MagicMock()
    cfg.validator_hotkey = "5" + "V" * 47
    cfg.netuid = 3
    cfg.koth_margin = 0.01
    cfg.koth_tail_size = 4
    cfg.koth_champion_share = 0.9
    cfg.koth_dethrone_z = 1.64
    cfg.koth_confirmation_seeds = 3
    cfg.miner_emission_share = 1.0
    cfg.burn_hotkey = "5Burn" + "x" * 43
    return ValidatorWorker(
        config=cfg,
        platform=MagicMock(),
        dittobench=MagicMock(),
        chain=MagicMock(),
        keypair=MagicMock(),
    )


class TestWorkerRescoreSweep:
    async def test_inert_when_ledger_has_no_versions(self) -> None:
        w = _worker()
        w._current_bench_version = 3
        w._confirm_and_submit = AsyncMock()
        ledger = SimpleNamespace(entries=[_e("a", 0.9), _e("b", 0.8)])
        w._platform.get_ledger = AsyncMock()
        out = await w._rescore_stale_champion_and_tail(ledger)
        # No version info ⇒ no re-score, no re-fetch, same ledger back.
        w._confirm_and_submit.assert_not_called()
        w._platform.get_ledger.assert_not_called()
        assert out is ledger

    async def test_rescore_then_refetch(self) -> None:
        w = _worker()
        w._current_bench_version = 3
        w._confirm_and_submit = AsyncMock(return_value=SimpleNamespace())
        stale_ledger = SimpleNamespace(
            entries=[
                _e("champ", 0.90, version=2, minutes=0),
                _e("r1", 0.70, version=2, minutes=1),
            ]
        )
        refreshed = SimpleNamespace(entries=[_e("champ", 0.55, version=3, minutes=0)])
        w._platform.get_ledger = AsyncMock(return_value=refreshed)

        out = await w._rescore_stale_champion_and_tail(stale_ledger)
        # Both stale (champion + tail) re-confirmed, then the ledger re-fetched.
        assert w._confirm_and_submit.await_count == 2
        w._platform.get_ledger.assert_awaited_once()
        assert out is refreshed

    async def test_current_version_ledger_not_rescored(self) -> None:
        w = _worker()
        w._current_bench_version = 3
        w._confirm_and_submit = AsyncMock()
        ledger = SimpleNamespace(entries=[_e("champ", 0.9, version=3)])
        w._platform.get_ledger = AsyncMock()
        out = await w._rescore_stale_champion_and_tail(ledger)
        w._confirm_and_submit.assert_not_called()
        assert out is ledger
