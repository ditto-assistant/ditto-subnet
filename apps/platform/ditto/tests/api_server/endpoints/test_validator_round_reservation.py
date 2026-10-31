"""Per-validator slot caps and reserved generator-round slots.

Validators differ widely in CPU, memory and disk, so one uniform cap is wrong
for most of them; and continual retests plus canonical work, left alone, fill
every slot and starve generator-competition rounds. These tests pin both
levers at the functions issuance itself calls:

* the per-validator cap replaces the global cap for exactly one hotkey and is
  still narrowed by what the validator advertises, the resource ceilings and
  the pause list;
* while rounds are enabled, ``min(R, allowed - 1)`` slots are held back from
  non-round work and stay available to round leases; with rounds disabled, or
  a single allowed slot, nothing is reserved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.generator import GeneratorCompetitionSettings
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketPurpose
from ditto.api_models.validator_slot_settings import (
    MAX_PER_VALIDATOR_OVERRIDES,
    ValidatorSlotOverride,
    ValidatorSlotSettings,
)
from ditto.api_server.dependencies import get_session
from ditto.api_server.endpoints import public as public_endpoint
from ditto.api_server.endpoints.validator import (
    _idle_retest_slot,
    _live_slot_leases,
    _round_reservation_declines,
)
from ditto.api_server.generator_competition import settings_checksum
from ditto.api_server.generator_round_reservation import (
    GeneratorRoundReservationResolver,
    round_reservation_active,
)
from ditto.api_server.validator_slot_settings import (
    HostResourceSample,
    allowed_slot_count,
    effective_max_concurrent_slots,
    reserved_round_slots,
    settings_from_row,
)
from ditto.db.models import (
    Agent,
    AgentStatus,
    GeneratorCompetitionSettingsRevision,
    TicketStatus,
    ValidatorHeartbeat,
    ValidatorSlotSettingsRevision,
    ValidatorTicket,
)

_BIG = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_SMALL = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
_ADMIN_TOKEN = "test-admin-token-at-least-32-characters"
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_LEGACY_REVISION = {
    "max_concurrent_slots": 4,
    "disk_percent_ceiling": 90,
    "memory_percent_ceiling": 90,
    "cpu_percent_ceiling": 0,
    "resource_block_percent_ceiling": 95,
    "paused_validator_hotkeys": [],
}

_ROUND_CAPABLE = {
    "screened_images": True,
    "require_screened_image": True,
    "source_build_fallback": False,
    "full_stack_managed": True,
    "stack_updater": True,
    "sandbox_egress_restricted": True,
    "executor_isolation": "rootless_dind",
    "scorer_benchmarks": {
        "status": "fresh_verified",
        "supported_bench_versions": [13],
        "observed_at": 1,
        "software_version": "1.2.3",
        "source_revision": "a" * 40,
        "generator_rounds": True,
    },
}
"""A signed capability set whose scorer advertises generator-round support."""


def _settings(**overrides: Any) -> ValidatorSlotSettings:
    return ValidatorSlotSettings.model_validate({**_LEGACY_REVISION, **overrides})


def _healthy() -> HostResourceSample:
    return HostResourceSample(cpu_percent=40, memory_percent=20, disk_percent=40)


class TestStoredRevisionsStillParse:
    def test_a_revision_written_before_the_new_fields_gets_their_defaults(
        self,
    ) -> None:
        row = ValidatorSlotSettingsRevision(
            revision=7, settings=dict(_LEGACY_REVISION), checksum="0" * 64
        )

        settings = settings_from_row(row)

        assert settings.max_concurrent_slots == 4
        assert settings.generator_round_reserved_slots == 1
        assert settings.per_validator == {}

    def test_overrides_round_trip_through_the_stored_json(self) -> None:
        settings = _settings(
            generator_round_reserved_slots=2,
            per_validator={
                _BIG: {"max_concurrent_slots": 6},
                _SMALL: {"generator_round_reserved_slots": 0},
            },
        )

        stored = settings.model_dump(mode="json")

        assert ValidatorSlotSettings.model_validate(stored) == settings
        assert stored["per_validator"][_BIG] == {
            "max_concurrent_slots": 6,
            "generator_round_reserved_slots": None,
        }

    @pytest.mark.parametrize(
        "per_validator",
        [
            {"not-a-hotkey": {"max_concurrent_slots": 2}},
            {_BIG: {"max_concurrent_slots": 0}},
            {_BIG: {"max_concurrent_slots": 9}},
            {_BIG: {"generator_round_reserved_slots": 9}},
            # An override that overrides nothing has one canonical spelling:
            # no entry at all.
            {_BIG: {}},
        ],
    )
    def test_malformed_overrides_are_refused(self, per_validator: Any) -> None:
        with pytest.raises(ValidationError):
            _settings(per_validator=per_validator)

    @pytest.mark.parametrize("reserved", [-1, 9])
    def test_the_global_reservation_is_bounded(self, reserved: int) -> None:
        with pytest.raises(ValidationError):
            _settings(generator_round_reserved_slots=reserved)

    def test_the_override_map_is_capped(self) -> None:
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

        def hotkey(index: int) -> str:
            return "5" + alphabet[index // 58] + alphabet[index % 58] + "A" * 45

        too_many = {
            hotkey(index): {"max_concurrent_slots": 2}
            for index in range(MAX_PER_VALIDATOR_OVERRIDES + 1)
        }
        with pytest.raises(ValidationError):
            _settings(per_validator=too_many)


class TestPerValidatorCap:
    def test_an_override_replaces_the_global_cap_for_that_hotkey_only(self) -> None:
        settings = _settings(
            max_concurrent_slots=2, per_validator={_BIG: {"max_concurrent_slots": 6}}
        )

        def allowed(hotkey: str | None) -> int:
            return allowed_slot_count(
                settings,
                advertised_slots=8,
                sample=_healthy(),
                validator_hotkey=hotkey,
            )

        assert allowed(_BIG) == 6
        assert allowed(_SMALL) == 2
        assert allowed(None) == 2

    def test_an_override_can_also_narrow_a_validator_below_the_fleet(self) -> None:
        settings = _settings(
            max_concurrent_slots=4, per_validator={_SMALL: {"max_concurrent_slots": 1}}
        )

        assert (
            allowed_slot_count(
                settings,
                advertised_slots=8,
                sample=_healthy(),
                validator_hotkey=_SMALL,
            )
            == 1
        )

    def test_an_override_never_grants_more_than_the_validator_advertises(
        self,
    ) -> None:
        settings = _settings(per_validator={_BIG: {"max_concurrent_slots": 8}})

        assert (
            allowed_slot_count(
                settings, advertised_slots=3, sample=_healthy(), validator_hotkey=_BIG
            )
            == 3
        )

    def test_the_resource_ceilings_still_bind_an_overridden_validator(self) -> None:
        settings = _settings(per_validator={_BIG: {"max_concurrent_slots": 8}})

        throttled = allowed_slot_count(
            settings,
            advertised_slots=8,
            sample=HostResourceSample(disk_percent=90),
            validator_hotkey=_BIG,
        )
        blocked = allowed_slot_count(
            settings,
            advertised_slots=8,
            sample=HostResourceSample(memory_percent=95),
            validator_hotkey=_BIG,
        )

        assert throttled == 1
        assert blocked == 0

    def test_an_override_that_only_sets_the_reservation_inherits_the_cap(
        self,
    ) -> None:
        settings = _settings(
            max_concurrent_slots=3,
            per_validator={_BIG: {"generator_round_reserved_slots": 2}},
        )

        assert effective_max_concurrent_slots(settings, validator_hotkey=_BIG) == 3


class TestReservedRoundSlots:
    @pytest.mark.parametrize(
        ("allowed", "configured", "expected"),
        [
            (4, 1, 1),
            (4, 3, 3),
            # At least one slot always stays for non-round work.
            (4, 8, 3),
            (2, 1, 1),
            # One allowed slot reserves nothing.
            (1, 1, 0),
            (1, 4, 0),
            (0, 1, 0),
            (6, 0, 0),
        ],
    )
    def test_min_of_configured_and_allowed_minus_one(
        self, allowed: int, configured: int, expected: int
    ) -> None:
        settings = _settings(generator_round_reserved_slots=configured)

        assert (
            reserved_round_slots(
                settings,
                validator_hotkey=_BIG,
                allowed_slots=allowed,
                rounds_active=True,
            )
            == expected
        )

    def test_a_host_s_only_healthy_slot_is_never_reserved(self) -> None:
        """Host-memory admission leaves a 16 GiB host one healthy slot under an
        override of 4; the round reservation must not take it from the quorum."""
        settings = _settings(generator_round_reserved_slots=1)

        def reserved(healthy: int | None) -> int:
            return reserved_round_slots(
                settings,
                validator_hotkey=_BIG,
                allowed_slots=4,
                rounds_active=True,
                healthy_slots=healthy,
            )

        assert reserved(1) == 0
        assert reserved(0) == 0
        assert reserved(3) == 1
        assert reserved(None) == 1

    def test_nothing_is_reserved_while_rounds_are_disabled(self) -> None:
        settings = _settings(generator_round_reserved_slots=3)

        assert (
            reserved_round_slots(
                settings,
                validator_hotkey=_BIG,
                allowed_slots=8,
                rounds_active=False,
            )
            == 0
        )

    def test_a_per_validator_reservation_overrides_the_default(self) -> None:
        settings = _settings(
            generator_round_reserved_slots=1,
            per_validator={
                _BIG: {"generator_round_reserved_slots": 3},
                _SMALL: {"generator_round_reserved_slots": 0},
            },
        )

        def reserved(hotkey: str) -> int:
            return reserved_round_slots(
                settings, validator_hotkey=hotkey, allowed_slots=6, rounds_active=True
            )

        assert reserved(_BIG) == 3
        assert reserved(_SMALL) == 0
        assert reserved("5" + "D" * 47) == 1


class TestCanonicalReservationGate:
    """The canonical job path's gate, called as the endpoint calls it."""

    def _declines(self, *, held: set[str], slot: str, running: bool = False) -> bool:
        return _round_reservation_declines(
            slot_id=slot,
            slot_running_benchmark=running,
            allowed_slots=4,
            reserved_slots=1,
            non_round_slots=held,
        )

    def test_four_allowed_one_reserved_admits_three_non_round_leases(self) -> None:
        assert not self._declines(held=set(), slot="slot-0")
        assert not self._declines(held={"slot-0"}, slot="slot-1")
        assert not self._declines(held={"slot-0", "slot-1"}, slot="slot-2")
        assert self._declines(held={"slot-0", "slot-1", "slot-2"}, slot="slot-3")

    def test_a_slot_resuming_its_own_lease_is_not_charged_against_itself(
        self,
    ) -> None:
        assert not self._declines(held={"slot-0", "slot-1", "slot-2"}, slot="slot-2")

    def test_a_running_slot_is_exempt(self) -> None:
        assert not self._declines(
            held={"slot-0", "slot-1", "slot-2"}, slot="slot-3", running=True
        )

    def test_no_reservation_never_declines(self) -> None:
        assert not _round_reservation_declines(
            slot_id="slot-3",
            slot_running_benchmark=False,
            allowed_slots=4,
            reserved_slots=0,
            non_round_slots={"slot-0", "slot-1", "slot-2"},
        )


def _heartbeat(*, slots: int = 4, round_capable: bool = True) -> Any:
    """The fields the slot picker reads, for a healthy accepting host."""
    return cast(
        Any,
        type(
            "Heartbeat",
            (),
            {
                "protocol_version": 32,
                "capabilities": _ROUND_CAPABLE if round_capable else None,
                "benchmark_capacity": {
                    "configured_slots": slots,
                    "healthy_slots": [f"slot-{index}" for index in range(slots)],
                    "admission": "accepting",
                    "active": [],
                },
                "system_metrics": {
                    "cpu_percent": 40,
                    "memory_percent": 20,
                    "disk_percent": 40,
                },
            },
        )(),
    )


async def _seed_leases(
    session: AsyncSession,
    *,
    now: datetime,
    leases: list[tuple[str, TicketPurpose]],
    hotkey: str = _BIG,
) -> None:
    for index, (slot, purpose) in enumerate(leases):
        agent_id = uuid4()
        session.add(
            Agent(
                agent_id=agent_id,
                miner_hotkey=f"round-reserve-miner-{index}-{agent_id.hex[:6]}",
                name=f"round-reserve-agent-{index}-{agent_id.hex[:6]}",
                sha256=agent_id.hex * 2,
                status=AgentStatus.EVALUATING,
                screening_policy_version=SCREENING_POLICY_VERSION,
            )
        )
        await session.flush()
        session.add(
            ValidatorTicket(
                agent_id=agent_id,
                bench_version=13,
                validator_hotkey=hotkey,
                slot_id=slot,
                status=TicketStatus.ISSUED,
                purpose=purpose,
                issued_at=now - timedelta(minutes=5),
                deadline=now + timedelta(minutes=60),
                attempt_count=1,
            )
        )
    await session.flush()


class TestIdleSlotPickerHonoursTheReservation:
    """Continual retests and confirmations go through ``_idle_retest_slot``;
    so do generator-round leases, on the ``round`` lane."""

    async def _pick(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        lane: str,
        rounds_active: bool,
        settings: ValidatorSlotSettings | None = None,
        slots: int = 4,
        round_capable: bool = True,
    ) -> str | None:
        return await _idle_retest_slot(
            session,
            heartbeat=_heartbeat(slots=slots, round_capable=round_capable),
            slot_settings=settings or _settings(max_concurrent_slots=4),
            validator_hotkey=_BIG,
            now=now,
            lane=cast(Any, lane),
            round_reservation_active=rounds_active,
        )

    async def test_three_non_round_leases_leave_the_fourth_slot_for_rounds(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        await _seed_leases(
            session,
            now=now,
            leases=[
                ("slot-0", TicketPurpose.CANONICAL_QUORUM),
                ("slot-1", TicketPurpose.CONTINUAL_RETEST),
                ("slot-2", TicketPurpose.CANONICAL_QUORUM),
            ],
        )

        assert (
            await self._pick(session, now=now, lane="non_round", rounds_active=True)
            is None
        )
        assert (
            await self._pick(session, now=now, lane="round", rounds_active=True)
            == "slot-3"
        )

    async def test_with_rounds_disabled_the_fourth_slot_is_ordinary_capacity(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        await _seed_leases(
            session,
            now=now,
            leases=[
                ("slot-0", TicketPurpose.CANONICAL_QUORUM),
                ("slot-1", TicketPurpose.CONTINUAL_RETEST),
                ("slot-2", TicketPurpose.CANONICAL_QUORUM),
            ],
        )

        assert (
            await self._pick(session, now=now, lane="non_round", rounds_active=False)
            == "slot-3"
        )

    async def test_a_validator_that_cannot_run_rounds_reserves_nothing(
        self, session: AsyncSession
    ) -> None:
        """No round lease could ever use the slot, so holding it would idle it."""
        now = datetime.now(UTC)
        await _seed_leases(
            session,
            now=now,
            leases=[
                ("slot-0", TicketPurpose.CANONICAL_QUORUM),
                ("slot-1", TicketPurpose.CONTINUAL_RETEST),
                ("slot-2", TicketPurpose.CANONICAL_QUORUM),
            ],
        )

        assert (
            await self._pick(
                session,
                now=now,
                lane="non_round",
                rounds_active=True,
                round_capable=False,
            )
            == "slot-3"
        )

    async def test_round_leases_do_not_consume_the_non_round_budget(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        await _seed_leases(
            session,
            now=now,
            leases=[
                ("slot-0", TicketPurpose.GENERATOR_ROUND),
                ("slot-1", TicketPurpose.GENERATOR_ROUND),
                ("slot-2", TicketPurpose.CANONICAL_QUORUM),
            ],
        )

        leases = await _live_slot_leases(session, validator_hotkey=_BIG, now=now)
        assert leases.slots == {"slot-0", "slot-1", "slot-2"}
        assert leases.non_round_slots == {"slot-2"}
        assert (
            await self._pick(session, now=now, lane="non_round", rounds_active=True)
            == "slot-3"
        )

    async def test_rounds_never_exceed_the_allowance(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        await _seed_leases(
            session,
            now=now,
            leases=[
                (f"slot-{index}", TicketPurpose.CANONICAL_QUORUM) for index in (0, 1, 2)
            ]
            + [("slot-3", TicketPurpose.GENERATOR_ROUND)],
        )

        assert (
            await self._pick(session, now=now, lane="round", rounds_active=True) is None
        )

    async def test_a_single_slot_validator_reserves_nothing(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)

        assert (
            await self._pick(
                session,
                now=now,
                lane="non_round",
                rounds_active=True,
                settings=_settings(max_concurrent_slots=1),
            )
            == "slot-0"
        )

    async def test_the_per_validator_cap_and_reservation_govern_the_picker(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        await _seed_leases(
            session,
            now=now,
            leases=[
                ("slot-0", TicketPurpose.CANONICAL_QUORUM),
                ("slot-1", TicketPurpose.CANONICAL_QUORUM),
            ],
        )
        # Global cap 2 would refuse a third lease outright; the override lifts
        # this validator to 6 and holds 2 of them back for rounds.
        settings = _settings(
            max_concurrent_slots=2,
            per_validator={
                _BIG: {"max_concurrent_slots": 6, "generator_round_reserved_slots": 2}
            },
        )

        assert (
            await self._pick(
                session,
                now=now,
                lane="non_round",
                rounds_active=True,
                settings=settings,
                slots=8,
            )
            == "slot-2"
        )

    async def test_a_paused_validator_gets_no_new_slot_on_either_lane(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        settings = _settings(paused_validator_hotkeys=[_BIG])

        for lane in ("round", "non_round"):
            assert (
                await self._pick(
                    session,
                    now=now,
                    lane=lane,
                    rounds_active=True,
                    settings=settings,
                )
                is None
            )


async def _write_generator_settings(
    maker: async_sessionmaker[AsyncSession], **fields: Any
) -> None:
    settings = GeneratorCompetitionSettings.model_validate(fields)
    async with maker() as session, session.begin():
        latest = await session.scalar(
            select(func.max(GeneratorCompetitionSettingsRevision.revision))
        )
        session.add(
            GeneratorCompetitionSettingsRevision(
                parent_revision=latest or 0,
                scope="*",
                settings=settings.model_dump(mode="json"),
                checksum=settings_checksum(settings),
                reason="toggle validator rounds for a reservation test",
                actor="backroom:test",
            )
        )


class TestReservationGate:
    """Reserved only while the competition is on and validator rounds are."""

    @pytest.mark.parametrize(
        ("fields", "expected"),
        [
            ({}, False),
            ({"mode": "shadow"}, False),
            ({"mode": "off", "validator_rounds_enabled": True}, False),
            ({"mode": "shadow", "validator_rounds_enabled": True}, True),
        ],
    )
    async def test_reads_the_governing_generator_revision(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        fields: dict[str, Any],
        expected: bool,
    ) -> None:
        if fields:
            await _write_generator_settings(session_maker, **fields)
        state = type("State", (), {})()
        async with session_maker() as session:
            assert await round_reservation_active(state, session) is expected

        state.session_maker = session_maker
        state.generator_round_reservation = GeneratorRoundReservationResolver()
        async with session_maker() as session:
            assert await round_reservation_active(state, session) is expected

    async def test_the_cache_is_invalidated_by_a_write(
        self, session_maker: async_sessionmaker[AsyncSession]
    ) -> None:
        resolver = GeneratorRoundReservationResolver(ttl_seconds=3600)
        assert await resolver.resolve(session_maker) is False

        await _write_generator_settings(
            session_maker, mode="shadow", validator_rounds_enabled=True
        )
        assert await resolver.resolve(session_maker) is False
        resolver.invalidate()
        assert await resolver.resolve(session_maker) is True


class TestPublicFleetView:
    def _row(self, hotkey: str, *, round_capable: bool = True) -> Any:
        now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
        return type(
            "Row",
            (),
            {
                "validator_hotkey": hotkey,
                "software_version": "0.40.0",
                "protocol_version": 16,
                "state": "idle",
                "active_agent_id": None,
                "system_metrics": {
                    "collected_at": int(now.timestamp()),
                    "cpu_percent": 40,
                    "memory_percent": 20,
                    "disk_percent": 40,
                    "docker": {
                        "status": "healthy",
                        "running_containers": 1,
                        "unhealthy_containers": 0,
                    },
                },
                "benchmark_progress": None,
                "benchmark_capacity": {
                    "configured_slots": 8,
                    "healthy_slots": [f"slot-{index}" for index in range(8)],
                    "admission": "accepting",
                    "active": [],
                },
                "capabilities": _ROUND_CAPABLE if round_capable else None,
                "stack": None,
                "stack_health": None,
                "first_seen_at": now - timedelta(days=3),
                "reported_at": now,
                "seen_at": now,
            },
        )()

    def _snapshot(
        self, *, rounds_active: bool, small_round_capable: bool = True
    ) -> dict[str, Any]:
        snapshot = public_endpoint._validator_heartbeats_response(
            rows=[
                self._row(_BIG),
                self._row(_SMALL, round_capable=small_round_capable),
            ],
            assignments=[],
            active_work=[],
            confirmation_work=[],
            orphaned_leases=[],
            now=datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
            active_bench_version=13,
            slot_settings=_settings(
                max_concurrent_slots=2,
                generator_round_reserved_slots=1,
                per_validator={
                    _BIG: {
                        "max_concurrent_slots": 6,
                        "generator_round_reserved_slots": 2,
                    }
                },
            ),
            round_reservation_active=rounds_active,
        )
        return {
            entry.validator_hotkey: (entry.allowed_slots, entry.reserved_round_slots)
            for entry in snapshot.validators
        }

    def test_publishes_the_per_validator_cap_and_reservation_dispatch_uses(
        self,
    ) -> None:
        assert self._snapshot(rounds_active=True) == {_BIG: (6, 2), _SMALL: (2, 1)}

    def test_publishes_no_reservation_while_rounds_are_disabled(self) -> None:
        assert self._snapshot(rounds_active=False) == {_BIG: (6, 0), _SMALL: (2, 0)}

    def test_publishes_no_reservation_for_a_validator_that_cannot_run_rounds(
        self,
    ) -> None:
        assert self._snapshot(rounds_active=True, small_round_capable=False) == {
            _BIG: (6, 2),
            _SMALL: (2, 0),
        }


class TestAdminSizingView:
    async def test_get_reports_each_validators_allowance_reservation_and_host(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from dataclasses import replace

        app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
        app.state.session_maker = session_maker

        async def _session() -> Any:
            async with session_maker() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        await _write_generator_settings(
            session_maker, mode="shadow", validator_rounds_enabled=True
        )
        now = datetime.now(UTC)
        offline_override = "5" + "E" * 47
        settings = _settings(
            max_concurrent_slots=2,
            per_validator={
                _BIG: ValidatorSlotOverride(
                    max_concurrent_slots=6, generator_round_reserved_slots=2
                ).model_dump(mode="json"),
                offline_override: {"max_concurrent_slots": 3},
            },
        )
        async with session_maker() as session, session.begin():
            session.add(
                ValidatorSlotSettingsRevision(
                    parent_revision=0,
                    scope="*",
                    settings=settings.model_dump(mode="json"),
                    checksum="1" * 64,
                    reason="size the big validator for a sizing-view test",
                    actor="backroom:test",
                )
            )
            session.add(
                ValidatorHeartbeat(
                    validator_hotkey=_BIG,
                    software_version="0.40.0",
                    protocol_version=32,
                    code_digest="a" * 64,
                    state="idle",
                    capabilities=_ROUND_CAPABLE,
                    system_metrics={
                        "cpu_percent": 40,
                        "memory_percent": 20,
                        "disk_percent": 40,
                    },
                    benchmark_capacity={
                        "configured_slots": 8,
                        "healthy_slots": [f"slot-{index}" for index in range(8)],
                        "admission": "accepting",
                        "active": [],
                    },
                    host_specs={
                        "cpu_count": 32,
                        "cpu_physical_cores": 16,
                        "memory_total_mib": 131072,
                        "disk_total_gib": 2000,
                        "architecture": "x86_64",
                    },
                    reported_at=now,
                    seen_at=now,
                    signature="b" * 128,
                )
            )
            await _seed_leases(
                session,
                now=now,
                leases=[
                    ("slot-0", TicketPurpose.CANONICAL_QUORUM),
                    ("slot-1", TicketPurpose.GENERATOR_ROUND),
                ],
            )

        response = await client.get(
            "/api/v1/admin/validator-slot-settings", headers=_ADMIN_HEADERS
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["generator_round_reservation_active"] is True
        fleet = {row["validator_hotkey"]: row for row in body["fleet"]}
        assert set(fleet) == {_BIG, offline_override}
        big = fleet[_BIG]
        assert big["online"] is True
        assert big["round_capable"] is True
        assert big["advertised_slots"] == 8
        assert big["max_concurrent_slots"] == 6
        assert big["allowed_slots"] == 6
        assert big["configured_reserved_round_slots"] == 2
        assert big["reserved_round_slots"] == 2
        assert big["non_round_slot_budget"] == 4
        assert big["live_leases_by_purpose"] == {
            "canonical_quorum": 1,
            "generator_round": 1,
        }
        assert big["host_specs"]["cpu_count"] == 32
        assert big["host_specs"]["memory_total_mib"] == 131072
        assert big["disk_percent"] == 40
        offline = fleet[offline_override]
        assert offline["seen_at"] is None
        assert offline["online"] is False
        assert offline["allowed_slots"] == 0
        assert offline["override"] == {
            "max_concurrent_slots": 3,
            "generator_round_reserved_slots": None,
        }


class TestWritesNeverSilentlyDropOverrides:
    async def test_a_client_that_omits_existing_overrides_is_refused(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        session_maker: async_sessionmaker[AsyncSession],
    ) -> None:
        from dataclasses import replace

        app.state.config = replace(app.state.config, admin_api_token=_ADMIN_TOKEN)
        app.state.session_maker = session_maker

        async def _session() -> Any:
            async with session_maker() as session:
                yield session

        app.dependency_overrides[get_session] = _session
        url = "/api/v1/admin/validator-slot-settings"
        with_override = {
            **_LEGACY_REVISION,
            "generator_round_reserved_slots": 1,
            "per_validator": {_BIG: {"max_concurrent_slots": 6}},
        }
        first = await client.post(
            url,
            headers=_ADMIN_HEADERS,
            json={
                "scope": "*",
                "expected_revision": 0,
                "settings": with_override,
                "reason": "lift the big validator to six slots",
                "actor": "backroom:test",
                "confirmation": "APPLY VALIDATOR SLOT CAP 4",
            },
        )
        assert first.status_code == 200, first.text
        assert (
            first.json()["settings"]["per_validator"][_BIG]["max_concurrent_slots"] == 6
        )

        stale = await client.post(
            url,
            headers=_ADMIN_HEADERS,
            json={
                "scope": "*",
                "expected_revision": 1,
                "settings": {**_LEGACY_REVISION, "max_concurrent_slots": 5},
                "reason": "a client that predates per-validator overrides",
                "actor": "backroom:test",
                "confirmation": "APPLY VALIDATOR SLOT CAP 5",
            },
        )
        assert stale.status_code == 409, stale.text
        assert "per_validator" in stale.text
