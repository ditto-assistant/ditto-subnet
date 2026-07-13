"""Common Random Numbers (CRN) seed derivation + plumbing.

``crn_seed`` must be pure and deterministic: every validator scoring the same set
of agents at the same bench_version derives the identical seed, so the champion
and its challengers face the same fresh dataset and their composites become
directly comparable. These tests pin the invariants
that keep it consensus-safe — determinism, order-independence, version rotation,
JSON-clean int63 range — and that the re-score sweep forwards ONE common seed.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

from ditto.validator.crn import confirmation_seeds, crn_seed
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


class TestConfirmationSeeds:
    def test_count_gives_distinct_deterministic_seeds(self) -> None:
        seeds = confirmation_seeds(["a", "b"], version=3, count=3)
        assert len(seeds) == 3
        assert len(set(seeds)) == 3  # the replicate index rotates each seed
        # Deterministic: every validator derives the identical set.
        assert seeds == confirmation_seeds(["b", "a"], version=3, count=3)

    def test_first_seed_is_the_classic_single_crn_seed(self) -> None:
        # k=0 must be byte-identical to the pre-P4 single-seed derivation so pins
        # and mixed K=1/K>1 fleets stay consistent.
        seeds = confirmation_seeds(["a", "b"], version=3, count=3)
        assert seeds[0] == crn_seed(["a", "b"], version=3)

    def test_count_one_or_less_degrades_to_one_classic_seed(self) -> None:
        assert confirmation_seeds(["a"], version=3, count=1) == [
            crn_seed(["a"], version=3)
        ]
        assert confirmation_seeds(["a"], version=3, count=0) == [
            crn_seed(["a"], version=3)
        ]

    def test_seeds_are_json_clean_int63(self) -> None:
        for s in confirmation_seeds(["a", "b", "c"], version=9, count=4):
            assert isinstance(s, int)
            assert 0 <= s <= _INT63_MAX


def _cfg() -> Any:
    cfg = MagicMock()
    cfg.validator_hotkey = "5" + "V" * 47
    cfg.netuid = 3
    cfg.koth_margin = 0.01
    cfg.koth_tail_size = 4
    cfg.koth_champion_share = 0.9
    cfg.koth_dethrone_z = 1.64
    cfg.koth_confirmation_seeds = 3
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
    async def test_all_stale_agents_get_the_same_common_confirmation_seeds(
        self,
    ) -> None:
        w = _worker()
        w._current_bench_version = 3
        w._confirm_and_submit = AsyncMock(return_value=SimpleNamespace())
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

        # Every stale agent is confirmed over the SAME set of common seeds...
        seed_sets = [
            tuple(c.kwargs["seeds"])
            for c in w._confirm_and_submit.await_args_list
        ]
        assert len(seed_sets) == 2  # champion + one tail agent
        assert seed_sets[0] == seed_sets[1]
        # ...which are exactly the deterministic K confirmation seeds for the set
        # (K=3 by config), with k=0 equal to the classic single CRN seed.
        expected = confirmation_seeds([str(champ), str(r1)], version=3, count=3)
        assert seed_sets[0] == tuple(expected)
        assert len(set(expected)) == 3
        assert expected[0] == crn_seed([str(champ), str(r1)], version=3)
