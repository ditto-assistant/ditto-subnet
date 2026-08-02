"""The operator slot cap that bounds concurrent benchmark leases per validator.

Validators advertise the slot capacity their host can offer; how much of that
the fleet actually uses is an operator decision that must be changeable from
backroom without a release. These tests pin the decision boundary itself --
how many concurrent leases are served, and what happens when the inputs are
missing or malformed -- because the failure mode of getting it wrong is
fleet-wide.

They call the endpoint's own :func:`_slot_cap_declines` rather than restating
the rule inline. An inline restatement is what let #454's assertion drift a
whole release behind the code it claimed to cover.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.validator_slot_settings import (
    CEILING_DISABLED,
    ValidatorSlotSettings,
)
from ditto.api_server.endpoints.validator import (
    _heartbeat_resource_sample,
    _held_lease_slots,
    _slot_cap_declines,
    _slot_ordinal,
    _validator_slot_settings,
)
from ditto.api_server.validator_slot_settings import (
    DEFAULT_SETTINGS,
    DISK_RESTRICTED_SLOTS,
    HostResourceSample,
    allowed_slot_count,
    blocked_resources,
    throttled_resources,
)
from ditto.db.models import Agent, AgentStatus, TicketStatus, ValidatorTicket

_HOTKEY = "5SlotCapValidatorHotkeyAAAAAAAAAAAAAAAAAAAAAAAAAA"
_OTHER_HOTKEY = "5SlotCapOtherValidatorHotkeyBBBBBBBBBBBBBBBBBBBBB"


def _sample(
    *, cpu: int | None = None, memory: int | None = None, disk: int | None = None
) -> HostResourceSample:
    """One heartbeat's host readings, defaulting every unnamed one to unknown."""
    return HostResourceSample(cpu_percent=cpu, memory_percent=memory, disk_percent=disk)


def _serves(
    slot_id: str, *, allowed: int, held: Collection[str] = (), running: bool = False
) -> bool:
    """The endpoint's own decision, inverted for readability."""
    return not _slot_cap_declines(
        slot_id=slot_id,
        slot_running_benchmark=running,
        allowed_slots=allowed,
        held_slots=held,
    )


class TestSlotOrdinal:
    @pytest.mark.parametrize(
        ("slot_id", "expected"),
        [("slot-0", 0), ("slot-1", 1), ("slot-7", 7)],
    )
    def test_reads_the_ordinal(self, slot_id: str, expected: int) -> None:
        assert _slot_ordinal(slot_id) == expected

    @pytest.mark.parametrize(
        "slot_id",
        ["", "slot", "slot-", "slot-x", "SLOT-1", "slot-01x", "slot--1", "0", "-1"],
    )
    def test_unparseable_ids_sort_above_every_cap(self, slot_id: str) -> None:
        """An unrecognised id must be declined, never read as slot zero."""
        assert not _serves(slot_id, allowed=8)

    def test_ordinals_outside_the_wire_contract_are_declined(self) -> None:
        """``^slot-[0-7]$`` is the protocol; nothing past it may hold a lease."""
        assert not _serves("slot-8", allowed=8)


class TestAllowedSlotCount:
    def test_default_policy_permits_two_slots(self) -> None:
        assert DEFAULT_SETTINGS.max_concurrent_slots == 2

    def test_cap_narrows_what_the_validator_advertises(self) -> None:
        settings = ValidatorSlotSettings(max_concurrent_slots=2)

        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=35))
            == 2
        )

    def test_cap_never_grants_more_than_advertised(self) -> None:
        """The platform may only ever narrow the host's own offer."""
        settings = ValidatorSlotSettings(max_concurrent_slots=8)

        assert (
            allowed_slot_count(settings, advertised_slots=1, sample=_sample(disk=35))
            == 1
        )

    def test_cap_of_one_restores_todays_behaviour(self) -> None:
        settings = ValidatorSlotSettings(max_concurrent_slots=1)
        allowed = allowed_slot_count(
            settings, advertised_slots=4, sample=_sample(disk=35)
        )

        assert _serves("slot-0", allowed=allowed)
        assert not _serves("slot-1", allowed=allowed, held={"slot-0"})

    def test_the_cap_counts_leases_not_ordinals(self) -> None:
        """Two leases held means no third, whichever ordinals carry them."""
        allowed = allowed_slot_count(
            ValidatorSlotSettings(max_concurrent_slots=2),
            advertised_slots=4,
            sample=_sample(disk=35),
        )

        assert not _serves("slot-3", allowed=allowed, held={"slot-0", "slot-1"})
        assert not _serves("slot-0", allowed=allowed, held={"slot-2", "slot-3"})

    def test_a_high_ordinal_is_served_when_the_validator_is_under_the_cap(
        self,
    ) -> None:
        """The regression: ``healthy_slots`` is sparse whenever a slot drains.

        ``5CqJAjSj`` advertises four slots with slot-0 unhealthy, so ordinals
        1-3 are all it can offer. Under an ordinal ceiling of three it could
        never reach the three leases the operator granted it, because slot-3
        was refused on its number alone.
        """
        allowed = allowed_slot_count(
            ValidatorSlotSettings(max_concurrent_slots=3),
            advertised_slots=4,
            sample=_sample(disk=80),
        )

        assert _serves("slot-3", allowed=allowed, held={"slot-1", "slot-2"})

    def test_a_slot_polling_for_its_own_live_lease_is_not_charged_for_it(
        self,
    ) -> None:
        """Resuming is downstream of this gate, so the gate must let it through."""
        assert _serves("slot-2", allowed=3, held={"slot-0", "slot-1", "slot-2"})
        assert not _serves("slot-3", allowed=3, held={"slot-0", "slot-1", "slot-2"})


class TestDiskCeiling:
    def test_a_nearly_full_host_is_held_to_one_slot(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=95,
        )

        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=90))
            == DISK_RESTRICTED_SLOTS
        )

    def test_a_host_below_the_ceiling_keeps_its_slots(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=2, disk_percent_ceiling=90
        )

        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=80))
            == 2
        )

    def test_unknown_disk_does_not_trip_the_breaker(self) -> None:
        """A validator reporting no metrics must not silently lose capacity."""
        settings = ValidatorSlotSettings(
            max_concurrent_slots=2, disk_percent_ceiling=90
        )

        assert not throttled_resources(settings, _sample(disk=None))
        assert not blocked_resources(settings, _sample(disk=None))
        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=None))
            == 2
        )

    def test_the_breaker_can_only_reduce_never_raise(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=1,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=CEILING_DISABLED,
        )

        assert (
            allowed_slot_count(settings, advertised_slots=1, sample=_sample(disk=95))
            == 1
        )


class TestMemoryAndCpuCeilings:
    """The disk breaker generalised: same tier, same arithmetic, more resources."""

    def test_memory_over_its_ceiling_throttles_like_disk(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            memory_percent_ceiling=90,
            resource_block_percent_ceiling=95,
        )

        assert throttled_resources(settings, _sample(memory=90)) == ("memory",)
        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(memory=90))
            == DISK_RESTRICTED_SLOTS
        )

    def test_a_busy_but_healthy_host_is_untouched(self) -> None:
        """The live fleet's worst memory reading is 65%. Nothing may change."""
        settings = DEFAULT_SETTINGS

        assert not throttled_resources(settings, _sample(memory=65, disk=45, cpu=5))
        assert not blocked_resources(settings, _sample(memory=65, disk=45, cpu=5))
        assert (
            allowed_slot_count(
                settings,
                advertised_slots=4,
                sample=_sample(memory=65, disk=45, cpu=5),
            )
            == settings.max_concurrent_slots
        )

    def test_cpu_is_disabled_by_default_in_both_tiers(self) -> None:
        """A pinned CPU is a working benchmark host, not a failing one."""
        pinned = _sample(cpu=100, memory=20, disk=45)

        assert DEFAULT_SETTINGS.cpu_percent_ceiling == CEILING_DISABLED
        assert not throttled_resources(DEFAULT_SETTINGS, pinned)
        assert not blocked_resources(DEFAULT_SETTINGS, pinned)
        assert (
            allowed_slot_count(DEFAULT_SETTINGS, advertised_slots=4, sample=pinned)
            == DEFAULT_SETTINGS.max_concurrent_slots
        )

    def test_cpu_gates_once_an_operator_enables_it(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            cpu_percent_ceiling=90,
            resource_block_percent_ceiling=95,
        )

        assert throttled_resources(settings, _sample(cpu=90)) == ("cpu",)
        assert blocked_resources(settings, _sample(cpu=95)) == ("cpu",)


class TestResourceBlockCeiling:
    """The refusal tier: an overloaded host receives nothing until it recovers."""

    def test_a_host_past_the_hard_stop_is_issued_nothing(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=95,
        )

        assert blocked_resources(settings, _sample(disk=95)) == ("disk",)
        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=95))
            == 0
        )

    def test_recovery_restores_full_capacity_with_no_operator_action(self) -> None:
        """The gate reads the newest heartbeat, so calming down is the reset."""
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=95,
        )

        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=95))
            == 0
        )
        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=90))
            == DISK_RESTRICTED_SLOTS
        )
        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=60))
            == 4
        )

    def test_the_hard_stop_ignores_resources_with_no_ceiling(self) -> None:
        """Disabling a resource disables it for both tiers, not just the throttle."""
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            disk_percent_ceiling=90,
            memory_percent_ceiling=CEILING_DISABLED,
            cpu_percent_ceiling=CEILING_DISABLED,
            resource_block_percent_ceiling=95,
        )

        assert blocked_resources(settings, _sample(memory=100, cpu=100)) == ()
        assert (
            allowed_slot_count(
                settings, advertised_slots=4, sample=_sample(memory=100, cpu=100)
            )
            == 4
        )

    def test_unknown_readings_never_block(self) -> None:
        """A pre-v3 heartbeat must keep working exactly as it always did."""
        assert not blocked_resources(DEFAULT_SETTINGS, _sample())
        assert (
            allowed_slot_count(DEFAULT_SETTINGS, advertised_slots=4, sample=_sample())
            == DEFAULT_SETTINGS.max_concurrent_slots
        )

    def test_a_blocked_validator_is_served_no_slot_at_all(self) -> None:
        """Zero allowed slots must reach the endpoint's own decision as a 204.

        This is the difference the operator asked for: not "one at a time", but
        "nothing until you have calmed down".
        """
        settings = ValidatorSlotSettings(
            max_concurrent_slots=8,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=95,
        )
        allowed = allowed_slot_count(
            settings, advertised_slots=8, sample=_sample(disk=95)
        )

        assert allowed == 0
        assert not _serves("slot-0", allowed=allowed)
        assert not _serves("slot-3", allowed=allowed, held={"slot-1"})

    def test_a_live_benchmark_still_resumes_while_blocked(self) -> None:
        """Blocking withholds NEW work; it never revokes a lease in flight.

        A 90-minute run that has already paid for its disk must be allowed to
        finish and report, or the block would cost exactly what it exists to
        prevent.
        """
        assert _serves("slot-2", allowed=0, held={"slot-2"}, running=True)

    def test_the_hard_stop_can_be_disabled_entirely(self) -> None:
        settings = ValidatorSlotSettings(
            max_concurrent_slots=4,
            disk_percent_ceiling=90,
            resource_block_percent_ceiling=CEILING_DISABLED,
        )

        assert blocked_resources(settings, _sample(disk=100)) == ()
        assert (
            allowed_slot_count(settings, advertised_slots=4, sample=_sample(disk=100))
            == DISK_RESTRICTED_SLOTS
        )


class TestLiveLeaseExemption:
    """Lowering the cap must cost the fleet new work only, never live work.

    Every path that resumes an in-flight lease sits downstream of the cap gate,
    so a slot already running a benchmark has to be let through. Without this,
    the instant-revert lever would strand a lease on each removed ordinal until
    it expired 90 minutes later, burning a retry attempt each time.
    """

    def test_a_running_slot_over_the_cap_is_still_served(self) -> None:
        held = {"slot-0", "slot-1", "slot-2", "slot-3"}

        assert _serves("slot-3", allowed=2, held=held, running=True)

    def test_an_idle_slot_over_the_cap_is_declined(self) -> None:
        assert not _serves("slot-3", allowed=2, held={"slot-0", "slot-1"})

    def test_dropping_the_cap_to_one_still_serves_every_running_slot(self) -> None:
        running = {"slot-0", "slot-1", "slot-2", "slot-3"}

        declined = [
            slot_id
            for slot_id in sorted(running)
            if not _serves(slot_id, allowed=1, held=running, running=True)
        ]

        assert declined == []

    def test_new_work_stops_immediately_when_the_cap_drops(self) -> None:
        held = {"slot-0", "slot-1", "slot-2", "slot-3"}

        served = [
            slot_id
            for slot_id in sorted(held)
            if _serves(slot_id, allowed=1, held=held, running=False)
        ]

        assert served == []


class TestHeartbeatResourceSample:
    def test_reads_the_reported_percentage(self) -> None:
        heartbeat = MagicMock(system_metrics={"disk_percent": 80})

        assert _heartbeat_resource_sample(heartbeat).disk_percent == 80

    @pytest.mark.parametrize(
        "metrics",
        [None, {}, {"disk_percent": None}, {"disk_percent": "80"}, "not-a-dict"],
    )
    def test_missing_or_malformed_metrics_read_as_unknown(
        self, metrics: object
    ) -> None:
        heartbeat = MagicMock(system_metrics=metrics)

        assert _heartbeat_resource_sample(heartbeat).disk_percent is None

    def test_absent_heartbeat_reads_as_unknown(self) -> None:
        assert _heartbeat_resource_sample(None).disk_percent is None

    def test_booleans_are_not_percentages(self) -> None:
        """``True`` is an int in Python; it must not be read as 1% disk use."""
        heartbeat = MagicMock(system_metrics={"disk_percent": True})

        assert _heartbeat_resource_sample(heartbeat).disk_percent is None


class TestResolverFailsClosed:
    async def test_a_missing_resolver_uses_the_conservative_default(self) -> None:
        """A DB or wiring failure must never uncap the fleet."""
        request = MagicMock()
        request.app.state = MagicMock(spec=[])

        assert await _validator_slot_settings(request) == DEFAULT_SETTINGS

    async def test_resolver_without_a_session_maker_uses_the_default(self) -> None:
        from ditto.api_server.validator_slot_settings import (
            ValidatorSlotSettingsResolver,
        )

        request = MagicMock()
        request.app.state.validator_slot_settings = ValidatorSlotSettingsResolver()
        request.app.state.session_maker = None

        assert await _validator_slot_settings(request) == DEFAULT_SETTINGS


class TestHeldLeaseSlots:
    """What the cap counts against: live leases, read from the ticket ledger.

    The gate runs *upstream* of the overdue sweep, so the query cannot lean on
    ``status`` alone -- an expired lease that no poll has swept yet is free
    capacity, and charging it would leave a validator short a slot for as long
    as the sweep lagged.
    """

    async def _seed(self, session: AsyncSession, *, now: datetime) -> None:
        agents = [uuid4() for _ in range(4)]
        for index, agent_id in enumerate(agents):
            session.add(
                Agent(
                    agent_id=agent_id,
                    miner_hotkey=f"slot-cap-miner-{index}",
                    name=f"slot-cap-agent-{index}",
                    sha256=f"{index}" * 64,
                    status=AgentStatus.EVALUATING,
                    screening_policy_version=SCREENING_POLICY_VERSION,
                )
            )
        session.add_all(
            [
                ValidatorTicket(
                    agent_id=agents[0],
                    bench_version=7,
                    validator_hotkey=_HOTKEY,
                    slot_id="slot-1",
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(minutes=5),
                    deadline=now + timedelta(minutes=60),
                    attempt_count=1,
                ),
                ValidatorTicket(
                    agent_id=agents[1],
                    bench_version=7,
                    validator_hotkey=_HOTKEY,
                    slot_id="slot-2",
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(minutes=5),
                    deadline=now + timedelta(minutes=60),
                    attempt_count=1,
                ),
                # Overdue but unswept: not occupied capacity.
                ValidatorTicket(
                    agent_id=agents[2],
                    bench_version=7,
                    validator_hotkey=_HOTKEY,
                    slot_id="slot-3",
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(hours=3),
                    deadline=now - timedelta(minutes=1),
                    attempt_count=1,
                ),
                # Another validator's lease must not be charged to this one.
                ValidatorTicket(
                    agent_id=agents[3],
                    bench_version=7,
                    validator_hotkey=_OTHER_HOTKEY,
                    slot_id="slot-0",
                    status=TicketStatus.ISSUED,
                    issued_at=now - timedelta(minutes=5),
                    deadline=now + timedelta(minutes=60),
                    attempt_count=1,
                ),
            ]
        )
        await session.flush()

    async def test_counts_only_this_validators_unexpired_leases(
        self, session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        await self._seed(session, now=now)

        assert await _held_lease_slots(session, validator_hotkey=_HOTKEY, now=now) == {
            "slot-1",
            "slot-2",
        }

    async def test_a_sparse_holder_under_the_cap_still_gets_its_last_slot(
        self, session: AsyncSession
    ) -> None:
        """End to end: slot-0 unhealthy, two leases held, cap of three.

        Production's ``5CqJAjSj`` in exactly this shape was refused slot-3 on
        its ordinal and capped at two concurrent benchmarks.
        """
        now = datetime.now(UTC)
        await self._seed(session, now=now)
        held = await _held_lease_slots(session, validator_hotkey=_HOTKEY, now=now)

        assert not _slot_cap_declines(
            slot_id="slot-3",
            slot_running_benchmark=False,
            allowed_slots=3,
            held_slots=held,
        )
