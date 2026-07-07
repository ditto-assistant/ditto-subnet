"""WP B9 — version-bump re-score sweep + version-aware weight fold.

Benchmark scores are only comparable within one ``bench_version`` (BENCHMARK-V2
§9). These tests cover the two subnet-side pieces: the fold ignores stale
versions, and the worker re-evaluates the stale champion + tail before folding.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.validator.weights import (
    agents_needing_rescore,
    compute_weights,
    filter_to_latest_version,
    max_bench_version,
)
from ditto.validator.worker import ValidatorWorker

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_KOTH: dict[str, Any] = {"margin": 0.01, "tail_size": 4, "champion_share": 0.9}


def _e(
    miner: str, composite: float, *, version: int | None = None, minutes: int = 0
) -> Any:
    """A duck-typed ledger entry. version=None models a platform that does not
    surface bench_version (the field is simply absent)."""
    ns = SimpleNamespace(
        miner_hotkey=miner,
        agent_id=uuid4(),
        composite=composite,
        first_seen=_T0 + timedelta(minutes=minutes),
        sha256="ab" * 32,
    )
    if version is not None:
        ns.bench_version = version
    return ns


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

    def test_fold_ignores_stale_versions(self) -> None:
        # A high-scoring v2 champion must NOT out-weigh the v3 cohort: v2 and v3
        # composites are incomparable, so only v3 is folded.
        entries = [
            _e("stale_champ", 0.99, version=2, minutes=0),
            _e("v3_champ", 0.50, version=3, minutes=1),
            _e("v3_runner", 0.40, version=3, minutes=2),
        ]
        w = compute_weights(entries, **_KOTH)
        assert "stale_champ" not in w
        assert w["v3_champ"] == pytest.approx(0.9)
        assert "v3_runner" in w


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
        w._evaluate_and_submit = AsyncMock()
        ledger = SimpleNamespace(entries=[_e("a", 0.9), _e("b", 0.8)])
        w._platform.get_ledger = AsyncMock()
        out = await w._rescore_stale_champion_and_tail(ledger)
        # No version info ⇒ no re-score, no re-fetch, same ledger back.
        w._evaluate_and_submit.assert_not_called()
        w._platform.get_ledger.assert_not_called()
        assert out is ledger

    async def test_rescore_then_refetch(self) -> None:
        w = _worker()
        w._current_bench_version = 3
        w._evaluate_and_submit = AsyncMock(return_value=None)
        stale_ledger = SimpleNamespace(
            entries=[
                _e("champ", 0.90, version=2, minutes=0),
                _e("r1", 0.70, version=2, minutes=1),
            ]
        )
        refreshed = SimpleNamespace(entries=[_e("champ", 0.55, version=3, minutes=0)])
        w._platform.get_ledger = AsyncMock(return_value=refreshed)

        out = await w._rescore_stale_champion_and_tail(stale_ledger)
        # Both stale (champion + tail) re-evaluated, then the ledger re-fetched.
        assert w._evaluate_and_submit.await_count == 2
        w._platform.get_ledger.assert_awaited_once()
        assert out is refreshed

    async def test_current_version_ledger_not_rescored(self) -> None:
        w = _worker()
        w._current_bench_version = 3
        w._evaluate_and_submit = AsyncMock()
        ledger = SimpleNamespace(entries=[_e("champ", 0.9, version=3)])
        w._platform.get_ledger = AsyncMock()
        out = await w._rescore_stale_champion_and_tail(ledger)
        w._evaluate_and_submit.assert_not_called()
        assert out is ledger
