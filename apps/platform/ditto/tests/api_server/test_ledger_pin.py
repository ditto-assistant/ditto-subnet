"""Epoch pin unit tests: digest, draft assembly, replay, and single-flight."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from ditto.api_models import LedgerEntry
from ditto.api_models.agent_status import AgentStatus
from ditto.api_server import ledger_pin as pin_mod
from ditto.api_server.ledger_pin import (
    LedgerPin,
    LedgerPinMaterializer,
    build_pin_draft,
    canonical_entries,
    ledger_digest,
    response_from_pin,
)
from ditto.chain.errors import ChainConnectionError
from ditto.chain.models import EpochSchedule

_NOW = datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
_HOTKEY_A = "5DhaT8U7LVwnnJNUU8VL1XEipicatoaDVVq7cHo227gogVZm"
_HOTKEY_B = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"


def _entry(
    hotkey: str, composite: float, *, first_seen: datetime, agent_id: UUID | None = None
) -> LedgerEntry:
    return LedgerEntry(
        miner_hotkey=hotkey,
        agent_id=agent_id or uuid4(),
        composite=composite,
        n=120,
        first_seen=first_seen,
        sha256="ab" * 32,
        run_id="run",
        seed=1,
        validator_hotkey=_VALIDATOR,
        status=AgentStatus.SCORED,
        bench_version=12,
    )


def _schedule(index: int = 25_028, *, block: int = 9_033_471) -> EpochSchedule:
    return EpochSchedule(
        netuid=118,
        subnet_epoch_index=index,
        last_epoch_block=9_033_469,
        pending_epoch_at=0,
        tempo=360,
        blocks_since_last_step=block - 9_033_469,
        block=block,
        block_hash="0x" + "ab" * 32,
        block_timestamp=1_789_000_000,
        next_epoch_block=9_033_829,
    )


def _snapshot(entries: list[LedgerEntry], **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "entries": entries,
        "generated_at": _NOW,
        "active_bench_version": 12,
        "burn_share": 0.1,
        "v9_confirmation_mode": None,
        "tie_weighting_mode": None,
        "dethrone_band_mode": "headroom_capped",
        "continual_retest_cohort_size": 5,
        "crown_mode": None,
        "owner_roots": {
            entry.agent_id: f"owner:{entry.miner_hotkey}" for entry in entries
        },
        "fleet_readiness": {"dethrone_band_clamp": True},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestDigest:
    def test_is_deterministic_and_covers_served_markers(self) -> None:
        entries = [_entry(_HOTKEY_A, 0.8, first_seen=_NOW)]
        encoded = canonical_entries(entries)
        served = {"burn_share": 0.1, "dethrone_band_mode": "headroom_capped"}
        assert ledger_digest(encoded, served) == ledger_digest(
            list(encoded), dict(served)
        )
        assert ledger_digest(encoded, {**served, "burn_share": 0.2}) != ledger_digest(
            encoded, served
        )
        assert len(ledger_digest(encoded, served)) == 64


class TestBuildPinDraft:
    def test_records_the_classic_champion_and_owner_root(self) -> None:
        senior = _entry(_HOTKEY_A, 0.80, first_seen=_NOW - timedelta(days=2))
        # Inside the decayed 0.007 band at 0.80, so first-seen keeps the crown.
        junior = _entry(_HOTKEY_B, 0.802, first_seen=_NOW - timedelta(days=1))
        draft = build_pin_draft(
            _schedule(),
            snapshot=_snapshot([junior, senior]),
            previous_pin=None,
            previous_champion_owner_root=None,
            now=_NOW,
        )
        assert draft.epoch_index == 25_028
        assert draft.pinned_block == 9_033_471
        assert draft.champion_agent_id == senior.agent_id
        assert draft.champion_owner_root == f"owner:{_HOTKEY_A}"
        assert draft.incumbent_agent_id is None
        assert draft.context["served"]["dethrone_band_mode"] == "headroom_capped"
        assert draft.context["served"]["crown_mode"] is None
        assert draft.ledger_digest == ledger_digest(
            canonical_entries([junior, senior]), draft.context["served"]
        )

    def test_incumbent_resolves_through_the_owner_family(self) -> None:
        """A resubmission keeps the crown: the family, not the agent id, carries it."""
        old_version = _entry(_HOTKEY_A, 0.80, first_seen=_NOW - timedelta(days=2))
        new_version = _entry(_HOTKEY_A, 0.79, first_seen=_NOW - timedelta(days=2))
        rival = _entry(_HOTKEY_B, 0.81, first_seen=_NOW - timedelta(days=1))
        draft = build_pin_draft(
            _schedule(25_029),
            snapshot=_snapshot([rival, new_version], crown_mode="incumbent"),
            previous_pin=None,
            previous_champion_owner_root=f"owner:{_HOTKEY_A}",
            now=_NOW,
        )
        assert old_version.agent_id != new_version.agent_id
        assert draft.incumbent_agent_id == new_version.agent_id
        assert draft.context["served"]["crown_mode"] == "incumbent"

    def test_lineage_gone_leaves_no_incumbent(self) -> None:
        rival = _entry(_HOTKEY_B, 0.81, first_seen=_NOW - timedelta(days=1))
        draft = build_pin_draft(
            _schedule(25_029),
            snapshot=_snapshot([rival], crown_mode="incumbent"),
            previous_pin=None,
            previous_champion_owner_root="owner:gone",
            now=_NOW,
        )
        assert draft.incumbent_agent_id is None
        assert draft.champion_agent_id == rival.agent_id


class TestResponseFromPin:
    def test_replays_frozen_markers_and_identity(self) -> None:
        entries = (_entry(_HOTKEY_A, 0.8, first_seen=_NOW),)
        pin = LedgerPin(
            netuid=118,
            epoch_index=25_028,
            last_epoch_block=9_033_469,
            pinned_block=9_033_471,
            pinned_block_hash="0x" + "ab" * 32,
            pinned_at=_NOW,
            bench_version=12,
            entries=entries,
            context={
                "served": {
                    "v9_confirmation_mode": None,
                    "tie_weighting_mode": "pool",
                    "dethrone_band_mode": "headroom_capped",
                    "burn_share": 0.25,
                    "continual_retest_cohort_size": 7,
                    "crown_mode": "incumbent",
                }
            },
            ledger_digest="cd" * 32,
            champion_agent_id=entries[0].agent_id,
            incumbent_agent_id=entries[0].agent_id,
        )
        fresh = response_from_pin(pin, stale=False, now=_NOW + timedelta(seconds=90))
        assert fresh.epoch_index == 25_028
        assert fresh.pinned_block == 9_033_471
        assert fresh.ledger_digest == "cd" * 32
        assert fresh.tie_weighting_mode == "pool"
        assert fresh.dethrone_band_mode == "headroom_capped"
        assert fresh.burn_share == 0.25
        assert fresh.continual_retest_cohort_size == 7
        assert fresh.crown_mode == "incumbent"
        assert fresh.crown_incumbent_agent_id == entries[0].agent_id
        assert fresh.stale is False
        assert fresh.age_seconds == 90
        assert fresh.generated_at == _NOW

        stale = response_from_pin(pin, stale=True, now=_NOW + timedelta(hours=2))
        assert stale.stale is True
        assert stale.age_seconds == 7200

    def test_incumbent_is_withheld_without_the_marker(self) -> None:
        entries = (_entry(_HOTKEY_A, 0.8, first_seen=_NOW),)
        pin = LedgerPin(
            netuid=118,
            epoch_index=1,
            last_epoch_block=0,
            pinned_block=1,
            pinned_block_hash="0x" + "ab" * 32,
            pinned_at=_NOW,
            bench_version=12,
            entries=entries,
            context={"served": {"crown_mode": None}},
            ledger_digest="cd" * 32,
            champion_agent_id=entries[0].agent_id,
            incumbent_agent_id=entries[0].agent_id,
        )
        replayed = response_from_pin(pin, stale=False, now=_NOW)
        assert replayed.crown_mode is None
        assert replayed.crown_incumbent_agent_id is None


class TestMaterializer:
    @pytest.mark.asyncio
    async def test_unreadable_schedule_is_not_cached(self) -> None:
        chain = SimpleNamespace(
            read_epoch_schedule=AsyncMock(side_effect=ChainConnectionError("down"))
        )
        app_state = SimpleNamespace(
            chain=chain, config=SimpleNamespace(chain=SimpleNamespace(netuid=118))
        )
        materializer = LedgerPinMaterializer()
        assert (
            await materializer.ensure(app_state, cast(Any, object()), now=_NOW) is None
        )
        assert materializer.newest_known is None
        chain.read_epoch_schedule.side_effect = None
        chain.read_epoch_schedule.return_value = _schedule()
        # The next call reads the chain again rather than remembering the failure.
        build = AsyncMock(return_value=None)
        materializer._load_or_build = build  # type: ignore[method-assign]
        await materializer.ensure(app_state, cast(Any, object()), now=_NOW)
        assert build.await_count == 1

    @pytest.mark.asyncio
    async def test_concurrent_callers_share_one_build_per_epoch(self) -> None:
        app_state = SimpleNamespace(
            chain=SimpleNamespace(
                read_epoch_schedule=AsyncMock(return_value=_schedule())
            ),
            config=SimpleNamespace(chain=SimpleNamespace(netuid=118)),
        )
        materializer = LedgerPinMaterializer()
        entries = (_entry(_HOTKEY_A, 0.8, first_seen=_NOW),)
        pin = LedgerPin(
            netuid=118,
            epoch_index=25_028,
            last_epoch_block=9_033_469,
            pinned_block=9_033_471,
            pinned_block_hash="0x" + "ab" * 32,
            pinned_at=_NOW,
            bench_version=12,
            entries=entries,
            context={"served": {}},
            ledger_digest="cd" * 32,
        )
        builds = 0

        async def _build(*_args: Any, **_kwargs: Any) -> LedgerPin:
            nonlocal builds
            builds += 1
            await asyncio.sleep(0.01)
            return pin

        materializer._load_or_build = _build  # type: ignore[method-assign]
        results = await asyncio.gather(
            *(
                materializer.ensure(app_state, cast(Any, object()), now=_NOW)
                for _ in range(5)
            )
        )
        assert all(result is pin for result in results)
        assert builds == 1
        assert materializer.newest_known is pin
        # A new epoch on chain invalidates the cache and builds again.
        app_state.chain.read_epoch_schedule.return_value = _schedule(25_029)
        await materializer.ensure(app_state, cast(Any, object()), now=_NOW)
        assert builds == 2

    @pytest.mark.asyncio
    async def test_build_errors_are_swallowed_and_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app_state = SimpleNamespace(
            chain=SimpleNamespace(
                read_epoch_schedule=AsyncMock(return_value=_schedule())
            ),
            config=SimpleNamespace(chain=SimpleNamespace(netuid=118)),
        )
        materializer = LedgerPinMaterializer()
        materializer._load_or_build = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        counted: list[str] = []

        class _Counter:
            def labels(self, *, outcome: str) -> _Counter:
                counted.append(outcome)
                return self

            def inc(self) -> None:
                return None

        monkeypatch.setattr(pin_mod, "LEDGER_PIN_MATERIALIZATIONS", _Counter())
        assert (
            await materializer.ensure(app_state, cast(Any, object()), now=_NOW) is None
        )
        assert counted == ["error"]
        assert materializer.newest_known is None
