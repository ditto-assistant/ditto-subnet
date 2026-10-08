"""Epoch pin unit tests: digest, draft assembly, replay, and single-flight."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    classify_vector_against_pins,
    ledger_digest,
    pin_expected_burn,
    pin_expected_shares,
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
    # Built through the wire parser, exactly as a pin is rehydrated from JSON.
    return LedgerEntry.model_validate(
        {
            "miner_hotkey": hotkey,
            "agent_id": str(agent_id or uuid4()),
            "composite": composite,
            "n": 120,
            "first_seen": first_seen.isoformat(),
            "sha256": "ab" * 32,
            "run_id": "run",
            "seed": 1,
            "validator_hotkey": _VALIDATOR,
            "status": AgentStatus.SCORED.value,
            "bench_version": 12,
        }
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
    @pytest.mark.parametrize(
        ("field", "value"),
        [("statistical_band_mode", "capped"), ("dethrone_seed_mode", "full_set")],
    )
    def test_consensus_marker_is_frozen_only_when_activated(
        self, field: str, value: str
    ) -> None:
        entry = _entry(_HOTKEY_A, 0.8, first_seen=_NOW)
        legacy = build_pin_draft(
            _schedule(),
            snapshot=_snapshot([entry]),
            previous_pin=None,
            previous_champion_owner_root=None,
            now=_NOW,
        )
        capped = build_pin_draft(
            _schedule(),
            snapshot=_snapshot([entry], **{field: value}),
            previous_pin=None,
            previous_champion_owner_root=None,
            now=_NOW,
        )

        assert field not in legacy.context["served"]
        assert capped.context["served"][field] == value
        assert capped.ledger_digest != legacy.ledger_digest

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


class TestPinAgreement:
    def _pin(self, entries: list[LedgerEntry]) -> SimpleNamespace:
        return SimpleNamespace(
            entries=canonical_entries(entries),
            context={"served": {"crown_mode": None}},
            incumbent_agent_id=None,
        )

    def test_expected_shares_follow_the_rank_schedule(self) -> None:
        champion = _entry(_HOTKEY_A, 0.80, first_seen=_NOW - timedelta(days=2))
        tail = _entry(_HOTKEY_B, 0.79, first_seen=_NOW - timedelta(days=1))
        shares = pin_expected_shares(self._pin([champion, tail]))
        assert shares is not None
        assert shares[_HOTKEY_A] == pytest.approx(0.65 / 0.79)
        assert shares[_HOTKEY_B] == pytest.approx(0.14 / 0.79)
        assert pin_expected_shares(self._pin([])) is None

    def test_classifies_current_previous_and_diverged(self) -> None:
        current = {_HOTKEY_A: 0.65 / 0.79, _HOTKEY_B: 0.14 / 0.79}
        previous = {_HOTKEY_B: 0.65 / 0.79, _HOTKEY_A: 0.14 / 0.79}
        burn = "5" + "Z" * 47
        # u16-quantized on-chain values, with a burn destination to ignore.
        revealed: dict[str, int | float] = {
            _HOTKEY_A: 42598,
            _HOTKEY_B: 9175,
            burn: 6553,
        }
        kwargs: dict[str, Any] = {
            "expected_current": current,
            "expected_previous": previous,
            "burn_hotkey": burn,
        }
        assert classify_vector_against_pins(revealed, **kwargs) == "current"
        assert (
            classify_vector_against_pins({_HOTKEY_B: 42598, _HOTKEY_A: 9175}, **kwargs)
            == "previous"
        )
        assert (
            classify_vector_against_pins({_HOTKEY_A: 30000, _HOTKEY_B: 30000}, **kwargs)
            == "diverged"
        )
        assert (
            classify_vector_against_pins({_HOTKEY_A: 1, "5" + "C" * 47: 1}, **kwargs)
            == "diverged"
        )
        assert classify_vector_against_pins({}, **kwargs) == "unknown"
        assert (
            classify_vector_against_pins(
                revealed,
                expected_current=None,
                expected_previous=None,
                burn_hotkey=burn,
            )
            == "unknown"
        )


_HOTKEY_C = "5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y"


def _legacy_expected_shares(pin: Any) -> dict[str, float] | None:
    """``pin_expected_shares`` exactly as it was before protocol 28."""
    from ditto.api_server.koth import (
        emission_allocation,
        koth_entries_from_ledger,
        project_koth,
    )

    entries = [LedgerEntry.model_validate(item) for item in (pin.entries or [])]
    served = pin.context.get("served", {})
    fold_entries = koth_entries_from_ledger(entries)
    tie_pooling = served.get("tie_weighting_mode") == "pool"
    clamp = served.get("dethrone_band_mode") == "headroom_capped"
    projection = project_koth(
        fold_entries,
        distinct_hotkeys=tie_pooling,
        ceiling_band_clamp=clamp,
        incumbent_agent_id=(
            pin.incumbent_agent_id if served.get("crown_mode") == "incumbent" else None
        ),
    )
    if projection is None:
        return None
    allocation = emission_allocation(
        fold_entries, projection, tie_pooling=tie_pooling, ceiling_band_clamp=clamp
    )
    total = sum(allocation.shares)
    shares: dict[str, float] = {}
    for member, share in zip(allocation.members, allocation.shares, strict=True):
        shares[member.miner_hotkey] = (
            shares.get(member.miner_hotkey, 0.0) + share / total
        )
    return shares


class TestProvisionalIncumbent:
    """Protocol 28: a held incumbent keeps the crown and its slot goes unpaid.

    ``held`` was the previous pin's champion; its owner has no payable
    generation left, only this withheld one. ``runner_up`` is eligible.
    """

    def _pair(self) -> tuple[LedgerEntry, LedgerEntry]:
        held = _entry(_HOTKEY_A, 0.95, first_seen=_NOW - timedelta(days=3))
        runner_up = _entry(_HOTKEY_B, 0.80, first_seen=_NOW - timedelta(days=2))
        return held, runner_up

    def _draft(
        self,
        payable: list[LedgerEntry],
        withheld: list[LedgerEntry],
        *,
        previous_root: str | None = f"owner:{_HOTKEY_A}",
        **overrides: Any,
    ) -> Any:
        values: dict[str, Any] = {
            "crown_mode": "incumbent",
            "reward_eligibility_mode": "enforce",
            "withheld_entries": withheld,
            "owner_roots": {
                entry.agent_id: f"owner:{entry.miner_hotkey}"
                for entry in (*payable, *withheld)
            },
        }
        values.update(overrides)
        return build_pin_draft(
            _schedule(25_029),
            snapshot=_snapshot(payable, **values),
            previous_pin=None,
            previous_champion_owner_root=previous_root,
            now=_NOW,
        )

    def _pin(self, draft: Any) -> LedgerPin:
        return LedgerPin(
            netuid=draft.netuid,
            epoch_index=draft.epoch_index,
            last_epoch_block=draft.last_epoch_block,
            pinned_block=draft.pinned_block,
            pinned_block_hash=draft.pinned_block_hash,
            pinned_at=draft.pinned_at,
            bench_version=draft.bench_version,
            entries=tuple(LedgerEntry.model_validate(item) for item in draft.entries),
            context=draft.context,
            ledger_digest=draft.ledger_digest,
            champion_agent_id=draft.champion_agent_id,
            incumbent_agent_id=draft.incumbent_agent_id,
        )

    def test_the_held_incumbent_keeps_the_crown_off_the_payable_entries(self) -> None:
        held, runner_up = self._pair()
        draft = self._draft([runner_up], [held])

        assert [item["agent_id"] for item in draft.entries] == [str(runner_up.agent_id)]
        served = draft.context["served"]
        assert served["provisional_incumbent"] == canonical_entries([held])[0]
        assert draft.incumbent_agent_id == held.agent_id
        # Crown and owner family carry to the next pin, so the crown survives.
        assert draft.champion_agent_id == held.agent_id
        assert draft.champion_owner_root == f"owner:{_HOTKEY_A}"
        # The provisional entry is fold input, so the digest covers it.
        assert draft.ledger_digest == ledger_digest(draft.entries, served)
        assert draft.ledger_digest != ledger_digest(
            draft.entries,
            {k: v for k, v in served.items() if k != "provisional_incumbent"},
        )

        replayed = response_from_pin(self._pin(draft), stale=False, now=_NOW)
        assert replayed.provisional_incumbent == held
        assert replayed.crown_incumbent_agent_id == held.agent_id
        assert [entry.agent_id for entry in replayed.entries] == [runner_up.agent_id]

    def test_expected_shares_leave_the_crown_unpaid_rather_than_reassigned(
        self,
    ) -> None:
        held, runner_up = self._pair()
        pin = self._pin(self._draft([runner_up], [held]))
        shares = pin_expected_shares(pin)
        # The runner-up keeps the tail share it holds while the crown is paid;
        # the remaining 0.65 / 0.79 of the miner pool burns.
        assert shares == {_HOTKEY_B: pytest.approx(0.14 / 0.79, abs=1e-15)}
        legacy = _legacy_expected_shares(
            SimpleNamespace(
                entries=canonical_entries([runner_up, held]),
                context={"served": {"crown_mode": "incumbent"}},
                incumbent_agent_id=held.agent_id,
            )
        )
        assert legacy is not None
        assert shares[_HOTKEY_B] == legacy[_HOTKEY_B]

        # The vector a protocol-28 validator submits for it: burn carries the
        # unpaid crown on top of the policy burn.
        burn = "5" + "Z" * 47
        revealed = {_HOTKEY_B: 0.14 / 0.79 * 0.9, burn: 1 - 0.14 / 0.79 * 0.9}
        assert (
            classify_vector_against_pins(
                revealed,
                expected_current=shares,
                expected_previous=None,
                burn_hotkey=burn,
            )
            == "current"
        )
        # A validator that crowned and paid the runner-up is visibly diverged.
        assert (
            classify_vector_against_pins(
                {_HOTKEY_B: 1.0},
                expected_current={_HOTKEY_A: 0.65 / 0.79, _HOTKEY_B: 0.14 / 0.79},
                expected_previous=None,
                burn_hotkey=burn,
            )
            == "diverged"
        )

    def test_a_challenger_that_clears_the_band_is_crowned_and_paid(self) -> None:
        held = _entry(_HOTKEY_A, 0.80, first_seen=_NOW - timedelta(days=3))
        challenger = _entry(_HOTKEY_B, 0.95, first_seen=_NOW - timedelta(days=1))
        draft = self._draft([challenger], [held])
        assert draft.incumbent_agent_id == held.agent_id
        assert draft.champion_agent_id == challenger.agent_id
        # The provisional incumbent falls to the tail, and that slot burns.
        shares = pin_expected_shares(self._pin(draft))
        assert shares == {_HOTKEY_B: pytest.approx(0.65 / 0.79, abs=1e-15)}

    def test_the_unpaid_slot_burns_on_top_of_the_owner_burn(self) -> None:
        held = _entry(_HOTKEY_A, 0.80, first_seen=_NOW - timedelta(days=3))
        challenger = _entry(_HOTKEY_B, 0.95, first_seen=_NOW - timedelta(days=1))
        shares = pin_expected_shares(self._pin(self._draft([challenger], [held])))
        # The source-emission collector checks the on-chain owner burn against
        # this: burn_share plus the unpaid tail slot, not burn_share alone.
        assert pin_expected_burn(0.2, shares) == pytest.approx(
            1 - 0.8 * (0.65 / 0.79), abs=1e-12
        )
        assert pin_expected_burn(0.2, {_HOTKEY_B: 0.6, _HOTKEY_C: 0.4}) == (
            pytest.approx(0.2, abs=1e-12)
        )
        assert pin_expected_burn(0.2, None) == pytest.approx(0.2, abs=1e-12)

    def test_only_the_incumbent_owner_can_be_provisional(self) -> None:
        held, runner_up = self._pair()
        other = _entry(_HOTKEY_C, 0.99, first_seen=_NOW - timedelta(days=4))
        # Another owner's withheld row never reaches the fold.
        draft = self._draft([runner_up], [other])
        assert "provisional_incumbent" not in draft.context["served"]
        assert draft.incumbent_agent_id is None
        assert draft.champion_agent_id == runner_up.agent_id

    @pytest.mark.parametrize(
        "overrides",
        [
            {"reward_eligibility_mode": None},
            {"crown_mode": None},
        ],
        ids=["gate-not-enforcing", "incumbency-off"],
    )
    def test_no_provisional_without_enforcement_and_incumbency(
        self, overrides: dict[str, Any]
    ) -> None:
        held, runner_up = self._pair()
        draft = self._draft([runner_up], [held], **overrides)
        assert "provisional_incumbent" not in draft.context["served"]
        assert draft.champion_agent_id == runner_up.agent_id

    def test_a_payable_generation_is_the_incumbent_not_the_withheld_one(
        self,
    ) -> None:
        held, runner_up = self._pair()
        older = _entry(_HOTKEY_A, 0.90, first_seen=_NOW - timedelta(days=5))
        draft = self._draft([older, runner_up], [held])
        assert draft.incumbent_agent_id == older.agent_id
        assert "provisional_incumbent" not in draft.context["served"]

    @pytest.mark.parametrize(
        ("case", "previous_root"),
        [
            ("payable-heir", f"owner:{_HOTKEY_A}"),
            ("no-previous-crown", None),
            ("other-owner-held", f"owner:{_HOTKEY_A}"),
        ],
    )
    def test_pins_without_a_provisional_incumbent_are_byte_identical(
        self, case: str, previous_root: str | None
    ) -> None:
        """Withheld entries on the snapshot change nothing unless one is the
        incumbent: same served markers, same digest, same crown, same replay,
        same expected shares as a snapshot that never carried them."""
        held, runner_up = self._pair()
        older = _entry(_HOTKEY_A, 0.90, first_seen=_NOW - timedelta(days=5))
        other = _entry(_HOTKEY_C, 0.99, first_seen=_NOW - timedelta(days=4))
        payable, withheld = {
            "payable-heir": ([older, runner_up], [held]),
            "no-previous-crown": ([runner_up], [held]),
            "other-owner-held": ([runner_up], [other]),
        }[case]
        baseline = build_pin_draft(
            _schedule(25_029),
            snapshot=_snapshot(
                payable, crown_mode="incumbent", reward_eligibility_mode="enforce"
            ),
            previous_pin=None,
            previous_champion_owner_root=previous_root,
            now=_NOW,
        )
        draft = self._draft(payable, withheld, previous_root=previous_root)
        assert draft.entries == baseline.entries
        assert draft.context == baseline.context
        assert draft.ledger_digest == baseline.ledger_digest
        assert draft.champion_agent_id == baseline.champion_agent_id
        assert draft.incumbent_agent_id == baseline.incumbent_agent_id
        pin = self._pin(draft)
        replayed = response_from_pin(pin, stale=False, now=_NOW)
        assert "provisional_incumbent" not in replayed.model_dump(mode="json")
        assert replayed == response_from_pin(self._pin(baseline), stale=False, now=_NOW)
        assert pin_expected_shares(pin) == _legacy_expected_shares(pin)

    def test_classification_of_ordinary_pins_is_unchanged(self) -> None:
        """No unpaid remainder, no rescaling: the comparison is the legacy one."""
        burn = "5" + "Z" * 47
        expected = {_HOTKEY_A: 0.65 / 0.79, _HOTKEY_B: 0.14 / 0.79}
        for a, b in ((42598, 9175), (42598 + 60, 9175), (42598 + 200, 9175)):
            revealed: dict[str, int | float] = {_HOTKEY_A: a, _HOTKEY_B: b, burn: 9}
            total = a + b
            legacy = all(
                abs(value / total - expected[h]) <= 0.002
                for h, value in ((_HOTKEY_A, a), (_HOTKEY_B, b))
            )
            verdict = classify_vector_against_pins(
                revealed,
                expected_current=expected,
                expected_previous=None,
                burn_hotkey=burn,
            )
            assert verdict == ("current" if legacy else "diverged")


class TestShadowTreasuryPin:
    def _treasury(self) -> dict:
        fixture = (
            Path(__file__).resolve().parents[5]
            / "packages/ditto-screening-protocol/tests/fixtures"
            / "treasury_ledger_pin_v1.json"
        )
        value = json.loads(fixture.read_text())
        value["identity"]["finalized_block"] = _schedule().block
        return value

    def _draft(self, treasury=None):
        return build_pin_draft(
            _schedule(),
            snapshot=_snapshot([], treasury_pin=treasury),
            previous_pin=None,
            previous_champion_owner_root=None,
            now=_NOW,
        )

    def _pin(self, draft):
        return LedgerPin(
            netuid=draft.netuid,
            epoch_index=draft.epoch_index,
            last_epoch_block=draft.last_epoch_block,
            pinned_block=draft.pinned_block,
            pinned_block_hash=draft.pinned_block_hash,
            pinned_at=draft.pinned_at,
            bench_version=draft.bench_version,
            entries=(),
            context=draft.context,
            ledger_digest=draft.ledger_digest,
        )

    def test_absence_preserves_legacy_digest_and_wire(self):
        legacy = build_pin_draft(
            _schedule(),
            snapshot=_snapshot([]),
            previous_pin=None,
            previous_champion_owner_root=None,
            now=_NOW,
        )
        explicit_none = self._draft()
        assert legacy.context == explicit_none.context
        assert legacy.ledger_digest == explicit_none.ledger_digest
        expected_served = {
            "v9_confirmation_mode": None,
            "tie_weighting_mode": None,
            "dethrone_band_mode": "headroom_capped",
            "burn_share": 0.1,
            "continual_retest_cohort_size": 5,
            "crown_mode": None,
        }
        assert legacy.context["served"] == expected_served
        # Fixed pre-treasury canonical JSON digest, independent of this builder.
        assert legacy.ledger_digest == (
            "d86e3fde7265152b526598c69b3e05caa39abedfaee3e49fa2d17c6dea9f55c0"
        )
        for draft in (legacy, explicit_none):
            response = response_from_pin(self._pin(draft), stale=False, now=_NOW)
            assert "treasury_pin" not in response.model_dump(mode="json")

    def test_policy_and_identity_are_frozen_in_digest_and_replay(self):
        raw = self._treasury()
        draft = self._draft(raw)
        stored = deepcopy(raw)
        raw["identity"]["uid"] += 1
        raw["policy"]["revision"] += 1
        assert draft.context["served"]["treasury_pin"] == stored
        assert draft.ledger_digest != self._draft().ledger_digest
        replay = response_from_pin(self._pin(draft), stale=True, now=_NOW)
        assert replay.treasury_pin.model_dump(mode="json") == stored
        assert replay.treasury_pin.mode == "shadow"
        changed = deepcopy(stored)
        changed["identity"]["uid"] += 1
        assert self._draft(changed).ledger_digest != draft.ledger_digest

    @pytest.mark.parametrize(
        "change",
        [
            "reused_uid",
            "owner",
            "chain",
            "policy",
            "future",
            "stale",
            "hash",
            "enforce",
        ],
    )
    def test_invalid_evidence_cannot_enter_a_new_pin(self, change):
        raw = self._treasury()
        if change == "reused_uid":
            raw["identity"]["uid_hotkey"] = "5" + "E" * 47
        elif change == "owner":
            raw["identity"]["subnet_owner_coldkey"] = raw["policy"]["collector_coldkey"]
        elif change == "chain":
            raw["identity"]["genesis_hash"] = "0x" + "22" * 32
        elif change == "policy":
            raw["policy"]["revision"] += 1
        elif change == "future":
            raw["identity"]["finalized_block"] += 1
        elif change == "stale":
            raw["identity"]["finalized_block"] = _schedule().last_epoch_block - 1
        elif change == "hash":
            raw["identity"]["finalized_block_hash"] = "0x" + "22" * 32
        else:
            raw["mode"] = "enforce"
        with pytest.raises(ValueError):
            self._draft(raw)

    def test_changed_stored_uid_cannot_replay_with_original_digest(self):
        pin = self._pin(self._draft(self._treasury()))
        pin.context["served"]["treasury_pin"]["identity"]["uid"] += 1
        with pytest.raises(ValueError, match="ledger digest mismatch"):
            response_from_pin(pin, stale=False, now=_NOW)

    def test_corrupt_stored_policy_is_not_silently_omitted(self):
        pin = self._pin(self._draft(self._treasury()))
        pin.context["served"]["treasury_pin"]["policy"]["revision"] += 1
        with pytest.raises(ValueError, match="policy digest mismatch"):
            response_from_pin(pin, stale=False, now=_NOW)

    def test_null_stored_evidence_is_not_treated_as_legacy_absence(self):
        pin = self._pin(self._draft(self._treasury()))
        pin.context["served"]["treasury_pin"] = None
        with pytest.raises(ValueError):
            response_from_pin(pin, stale=False, now=_NOW)
