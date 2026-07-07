"""v3 #1 — Common Random Numbers (CRN) seed derivation + plumbing.

``crn_seed`` must be pure and deterministic: every validator scoring the same set
of agents at the same bench_version derives the identical seed, so the champion
and its challengers face the same fresh dataset and their composites become
directly comparable (BENCHMARK-V3-IDEAS.md §2.1). These tests pin the invariants
that keep it consensus-safe — determinism, order-independence, version rotation,
JSON-clean int63 range — and that the re-score sweep forwards ONE common seed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from ditto.validator.crn import crn_seed
from ditto.validator.worker import ValidatorWorker

_INT63_MAX = (1 << 63) - 1


class TestCrnSeed:
    def test_deterministic(self) -> None:
        ids = ["a", "b", "c"]
        assert crn_seed(ids, version=3) == crn_seed(ids, version=3)

    def test_order_independent(self) -> None:
        # The *set* of compared agents fixes the seed, not the iteration order.
        assert crn_seed(["a", "b", "c"], version=3) == crn_seed(
            ["c", "a", "b"], version=3
        )

    def test_version_rotates_the_seed(self) -> None:
        ids = ["a", "b"]
        assert crn_seed(ids, version=3) != crn_seed(ids, version=4)

    def test_different_agent_set_rotates_the_seed(self) -> None:
        assert crn_seed(["a", "b"], version=3) != crn_seed(["a", "c"], version=3)

    def test_single_agent_stable(self) -> None:
        assert crn_seed(["a"], version=3) == crn_seed(["a"], version=3)

    def test_int63_range(self) -> None:
        # JSON-clean, never negative — mirrors dittobench-api FreshSeed.
        for ids, ver in ([""], 0), (["a"], 3), (["a", "b", "zzz"], 99):
            s = crn_seed(ids, version=ver)
            assert isinstance(s, int)
            assert 0 <= s <= _INT63_MAX

    def test_consumes_a_generator_once(self) -> None:
        # Passing a one-shot iterable must still sort/hash correctly.
        gen = (x for x in ["b", "a"])
        assert crn_seed(gen, version=1) == crn_seed(["a", "b"], version=1)

    def test_version_coerced_to_int(self) -> None:
        # Bench versions arrive as ints; a float that is integral hashes the same.
        assert crn_seed(["a"], version=3) == crn_seed(["a"], version=int(3.0))


def _cfg() -> Any:
    cfg = MagicMock()
    cfg.validator_hotkey = "5" + "V" * 47
    cfg.netuid = 3
    cfg.koth_margin = 0.01
    cfg.koth_tail_size = 4
    cfg.koth_champion_share = 0.9
    cfg.koth_dethrone_z = 1.64
    return cfg


def _worker() -> Any:
    return ValidatorWorker(
        config=_cfg(),
        platform=MagicMock(),
        dittobench=MagicMock(),
        chain=MagicMock(),
        keypair=MagicMock(),
    )


def _entry(aid: UUID, composite: float, *, version: int, minutes: int) -> Any:
    from datetime import UTC, datetime, timedelta

    return SimpleNamespace(
        miner_hotkey=f"hk-{aid}",
        agent_id=aid,
        sha256="ab" * 32,
        composite=composite,
        bench_version=version,
        first_seen=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=minutes),
    )


class TestRescoreSweepUsesCommonSeed:
    async def test_all_stale_agents_get_one_common_crn_seed(self) -> None:
        w = _worker()
        w._current_bench_version = 3
        w._evaluate_and_submit = AsyncMock(return_value=None)
        champ, r1 = uuid4(), uuid4()
        stale_ledger = SimpleNamespace(
            entries=[
                _entry(champ, 0.90, version=2, minutes=0),
                _entry(r1, 0.70, version=2, minutes=1),
            ]
        )
        w._platform.get_ledger = AsyncMock(
            return_value=SimpleNamespace(
                entries=[_entry(champ, 0.55, version=3, minutes=0)]
            )
        )

        await w._rescore_stale_champion_and_tail(stale_ledger)

        # Every stale agent scored on the SAME seed...
        seeds = {c.kwargs["seed"] for c in w._evaluate_and_submit.await_args_list}
        assert len(seeds) == 1
        # ...and that seed is exactly the deterministic CRN seed for the set.
        expected = crn_seed([str(champ), str(r1)], version=3)
        assert seeds == {expected}
