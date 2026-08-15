"""The public fleet view reports authorized slots, not just advertised ones.

A validator advertises its own capacity in the heartbeat; the operator cap
decides how much of that capacity dispatch will actually fund. Publishing only
the advertised number made an eight-slot validator under a cap of six read as
six idle machines plus two more waiting to be used -- capacity that no ticket
would ever reach. These tests pin the published number to the very function
ticket issuance calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ditto.api_models.validator_slot_settings import ValidatorSlotSettings
from ditto.api_server.endpoints import public as public_endpoint

_VALIDATOR = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
_NOW = datetime(2026, 7, 27, 17, 0, tzinfo=UTC)


def _row(
    *,
    configured_slots: int,
    disk_percent: int = 45,
    healthy_slots: list[str] | None = None,
) -> SimpleNamespace:
    """One stored heartbeat advertising ``configured_slots`` healthy slots."""
    slots = (
        healthy_slots
        if healthy_slots is not None
        else [f"slot-{index}" for index in range(configured_slots)]
    )
    return SimpleNamespace(
        validator_hotkey=_VALIDATOR,
        software_version="0.36.0",
        protocol_version=16,
        state="idle",
        active_agent_id=None,
        system_metrics={
            "collected_at": int(_NOW.timestamp()),
            "cpu_percent": 0,
            "memory_percent": 20,
            "disk_percent": disk_percent,
            "docker": {
                "status": "healthy",
                "running_containers": 1,
                "unhealthy_containers": 0,
            },
        },
        benchmark_progress=None,
        benchmark_capacity={
            "configured_slots": configured_slots,
            "healthy_slots": slots,
            "admission": "accepting",
            "active": [],
        },
        capabilities=None,
        stack=None,
        stack_health=None,
        first_seen_at=_NOW - timedelta(days=13),
        reported_at=_NOW,
        seen_at=_NOW,
    )


def _snapshot(row: SimpleNamespace, settings: ValidatorSlotSettings):
    return public_endpoint._validator_heartbeats_response(
        rows=[row],
        assignments=[],
        active_work=[],
        confirmation_work=[],
        orphaned_leases=[],
        now=_NOW,
        active_bench_version=7,
        slot_settings=settings,
    )


class TestAuthorizedSlotReporting:
    def test_an_issuance_pause_is_visible_and_funds_no_new_slots(self) -> None:
        """The Backroom brake must be visible in the same snapshot it controls."""
        snapshot = _snapshot(
            _row(configured_slots=8),
            ValidatorSlotSettings(
                max_concurrent_slots=8,
                disk_percent_ceiling=90,
                paused_validator_hotkeys=[_VALIDATOR],
            ),
        )

        entry = snapshot.validators[0]
        assert entry.online is True
        assert entry.availability == "available"
        assert entry.issuance_paused is True
        assert entry.configured_slots == 8
        assert entry.allowed_slots == 0

    def test_the_cap_narrows_what_the_validator_advertises(self) -> None:
        """Eight advertised under a cap of six is six -- the fleet's own case."""
        snapshot = _snapshot(
            _row(configured_slots=8),
            ValidatorSlotSettings(max_concurrent_slots=6, disk_percent_ceiling=90),
        )

        entry = snapshot.validators[0]
        assert entry.configured_slots == 8
        assert entry.allowed_slots == 6

    def test_the_cap_never_widens_beyond_what_is_advertised(self) -> None:
        """The policy is a ceiling, not a grant: it cannot invent a slot."""
        snapshot = _snapshot(
            _row(configured_slots=4),
            ValidatorSlotSettings(max_concurrent_slots=8, disk_percent_ceiling=90),
        )

        assert snapshot.validators[0].allowed_slots == 4

    def test_a_tripped_disk_ceiling_publishes_the_single_slot_it_leaves(self) -> None:
        """The breaker holds a validator to one slot; the view must say so."""
        snapshot = _snapshot(
            _row(configured_slots=8, disk_percent=90),
            ValidatorSlotSettings(max_concurrent_slots=6, disk_percent_ceiling=90),
        )

        assert snapshot.validators[0].allowed_slots == 1

    def test_capacity_absent_reads_as_the_single_slot_it_implies(self) -> None:
        """No parseable capacity is one slot, exactly as dispatch assumes."""
        row = _row(configured_slots=4)
        row.benchmark_capacity = None
        snapshot = _snapshot(
            row, ValidatorSlotSettings(max_concurrent_slots=6, disk_percent_ceiling=90)
        )

        entry = snapshot.validators[0]
        assert entry.configured_slots == 1
        assert entry.allowed_slots == 1

    def test_the_policy_travels_with_the_snapshot(self) -> None:
        """A reader can explain the number without re-deriving the policy."""
        snapshot = _snapshot(
            _row(configured_slots=8),
            ValidatorSlotSettings(max_concurrent_slots=3, disk_percent_ceiling=75),
        )

        assert snapshot.slot_policy.max_concurrent_slots == 3
        assert snapshot.slot_policy.disk_percent_ceiling == 75

    def test_a_disabled_disk_ceiling_travels_with_the_snapshot(self) -> None:
        """Zero remains the public disabled value even when disk is saturated."""
        snapshot = _snapshot(
            _row(configured_slots=8, disk_percent=100),
            ValidatorSlotSettings(max_concurrent_slots=8, disk_percent_ceiling=0),
        )

        assert snapshot.slot_policy.disk_percent_ceiling == 0
        assert snapshot.validators[0].allowed_slots == 8
