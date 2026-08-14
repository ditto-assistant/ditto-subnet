"""Classification and privacy tests for public fleet-health reporting."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from ditto.api_models.public import PublicSystemMetrics
from ditto.api_server.endpoints.public import (
    _BENCHMARK_STALL_AFTER,
    _benchmark_stalled,
    _fleet_classification,
    _public_system_metrics,
    _validator_heartbeats_response,
)
from ditto.api_server.validator_slot_settings import (
    DEFAULT_SETTINGS as SLOT_SETTINGS_DEFAULT,
)
from ditto.db.queries.orphaned_leases import OrphanedLease

# Real hotkeys from the 2026-07-27 eviction incident: the wire model pins
# validator hotkeys to the SS58 shape, so a placeholder will not serialize.
_HOTKEY = "5HmP9732JFjnut2RY9yg4Gz2qJ38vF8xFwZb5dQVPF7FsmZz"
_OTHER_HOTKEY = "5CFtzzb4vym9eysfeF9cxxp6D7gksuUVTKYNq1mchnrMs118"


@pytest.fixture
def healthy_metrics() -> PublicSystemMetrics:
    return PublicSystemMetrics(
        cpu_percent=20,
        memory_percent=40,
        disk_percent=55,
        docker_status="healthy",
        running_containers=3,
        unhealthy_containers=0,
    )


@pytest.mark.parametrize(
    ("state", "age", "metrics_kind", "expected"),
    [
        ("idle", timedelta(seconds=30), "healthy", (True, "available", "healthy")),
        ("idle", timedelta(seconds=30), "warning", (True, "available", "warning")),
        ("idle", timedelta(minutes=6), "healthy", (False, "stale", "healthy")),
        ("idle", timedelta(minutes=16), "healthy", (False, "offline", "healthy")),
        ("paused", timedelta(seconds=30), "healthy", (True, "paused", "healthy")),
        ("idle", timedelta(seconds=30), "missing", (True, "available", "unknown")),
        ("idle", timedelta(seconds=30), "partial", (True, "available", "unknown")),
    ],
)
def test_classifies_availability_without_turning_missing_metrics_into_outage(
    healthy_metrics: PublicSystemMetrics,
    state: str,
    age: timedelta,
    metrics_kind: str,
    expected: tuple[bool, str, str],
) -> None:
    now = datetime.now(UTC)
    metrics: PublicSystemMetrics | None = healthy_metrics
    if metrics_kind == "warning":
        metrics = healthy_metrics.model_copy(update={"disk_percent": 95})
    elif metrics_kind == "missing":
        metrics = None
    elif metrics_kind == "partial":
        metrics = healthy_metrics.model_copy(update={"docker_status": "unavailable"})
    assert (
        _fleet_classification(
            state=state,
            seen_at=now - age,
            now=now,
            metrics=metrics,
        )
        == expected
    )


def test_disk_usage_below_warning_threshold_is_healthy(
    healthy_metrics: PublicSystemMetrics,
) -> None:
    now = datetime.now(UTC)
    metrics = healthy_metrics.model_copy(update={"disk_percent": 90})

    assert _fleet_classification(
        state="idle",
        seen_at=now - timedelta(seconds=30),
        now=now,
        metrics=metrics,
    ) == (True, "available", "healthy")


def test_saturated_cpu_is_healthy_workload(
    healthy_metrics: PublicSystemMetrics,
) -> None:
    now = datetime.now(UTC)
    busy_metrics = healthy_metrics.model_copy(update={"cpu_percent": 100})

    assert _fleet_classification(
        state="running_benchmark",
        seen_at=now - timedelta(seconds=30),
        now=now,
        metrics=busy_metrics,
    ) == (True, "available", "healthy")


def test_malformed_stored_metrics_are_not_partially_exposed() -> None:
    raw = {
        "collected_at": int(datetime.now(UTC).timestamp()),
        "cpu_percent": 20,
        "memory_percent": 40,
        "disk_percent": 55,
        "docker": {
            "status": "healthy",
            "running_containers": 3,
            "unhealthy_containers": 0,
        },
        "hostname": "private-host",
    }
    assert _public_system_metrics(raw) is None


def test_early_stage_past_threshold_is_stalled() -> None:
    now = datetime.now(UTC)
    started = now - _BENCHMARK_STALL_AFTER - timedelta(seconds=1)
    for stage in ("preparing", "building_harness", "starting_harness"):
        assert _benchmark_stalled(stage, started, now) is True


def test_recent_early_stage_is_not_stalled() -> None:
    now = datetime.now(UTC)
    started = now - timedelta(minutes=2)
    assert _benchmark_stalled("building_harness", started, now) is False


def test_running_benchmark_is_never_stalled_on_wall_clock_alone() -> None:
    # A long-running benchmark can legitimately run to the 75-minute cap, so
    # elapsed time by itself must never flag it. It is now judged against its own
    # reported check count instead (see TestBenchmarkStallDetection in
    # test_public.py); with no count reported there is nothing to judge, and an
    # absent report must not be read as a wedged run.
    now = datetime.now(UTC)
    started = now - timedelta(hours=1)
    assert _benchmark_stalled("running_benchmark", started, now) is False
    assert (
        _benchmark_stalled("running_benchmark", started, now, completed=None) is False
    )
    assert _benchmark_stalled(None, started, now) is False
    # A count that has plainly not kept up with the clock is the one case that
    # does flag: 3 checks cannot account for an hour.
    assert _benchmark_stalled("running_benchmark", started, now, completed=3) is True


class TestOrphanedSlotSerialization:
    """A slot the platform evicted while the validator kept running must never
    reconcile to a clean "idle".

    The serializer cannot derive this from its other two inputs, which is the
    whole reason it takes a third: the evicted lease is gone from
    ``assignments`` by construction, and the ingest filter drops its slot from
    the stored capacity that backs ``active_work``. Both halves therefore agree
    the slot is free while the host is still burning a benchmark's worth of CPU
    on it.
    """

    @staticmethod
    def _heartbeat_row(hotkey: str = _HOTKEY) -> SimpleNamespace:
        now = datetime.now(UTC)
        return SimpleNamespace(
            validator_hotkey=hotkey,
            software_version="0.35.2",
            protocol_version=16,
            code_digest="ab" * 32,
            state="running_benchmark",
            active_agent_id=None,
            first_seen_at=now - timedelta(days=1),
            reported_at=now - timedelta(seconds=5),
            seen_at=now - timedelta(seconds=5),
            system_metrics=None,
            capabilities=None,
            stack=None,
            stack_health=None,
            benchmark_capacity={
                "configured_slots": 2,
                "healthy_slots": ["slot-0", "slot-1"],
                "admission": "accepting",
                "active": [],
            },
            claimed_slots=[{"slot_id": "slot-0", "agent_id": str(uuid4())}],
        )

    @staticmethod
    def _orphan(state: str, *, slot_id: str = "slot-0") -> OrphanedLease:
        now = datetime.now(UTC)
        return OrphanedLease(
            audit_id=uuid4(),
            validator_hotkey=_HOTKEY,
            slot_id=slot_id,
            agent_id=uuid4(),
            agent_name="mnemox-v55",
            bench_version=7,
            state=state,  # type: ignore[arg-type]
            reason="validator_still_claims_slot",
            evicted_at=now - timedelta(minutes=23),
            orphaned_for_seconds=1380.0,
            original_deadline=now + timedelta(minutes=44),
            protocol_version=16,
        )

    def test_an_orphaned_slot_is_published_on_its_validator(self) -> None:
        response = _validator_heartbeats_response(
            rows=[self._heartbeat_row()],
            assignments=[],
            active_work=[],
            confirmation_work=[],
            orphaned_leases=[self._orphan("still_running")],
            now=datetime.now(UTC),
            active_bench_version=7,
            slot_settings=SLOT_SETTINGS_DEFAULT,
        )

        [entry] = response.validators
        assert [(slot.slot_id, slot.state) for slot in entry.orphaned_slots] == [
            ("slot-0", "still_running")
        ]
        assert entry.orphaned_slots[0].agent_name == "mnemox-v55"
        assert entry.orphaned_slots[0].orphaned_for_seconds == 1380.0
        # The lie this exists to stop: the same snapshot reconciles to no
        # assignment and no active work, so nothing else on the entry says the
        # host is busy.
        assert entry.active_benchmarks == []
        assert entry.assigned_benchmarks == []

    def test_an_indeterminate_slot_is_published_rather_than_guessed_away(
        self,
    ) -> None:
        response = _validator_heartbeats_response(
            rows=[self._heartbeat_row()],
            assignments=[],
            active_work=[],
            confirmation_work=[],
            orphaned_leases=[self._orphan("indeterminate")],
            now=datetime.now(UTC),
            active_bench_version=7,
            slot_settings=SLOT_SETTINGS_DEFAULT,
        )

        [entry] = response.validators
        assert [slot.state for slot in entry.orphaned_slots] == ["indeterminate"]

    def test_a_released_slot_keeps_rendering_as_idle(self) -> None:
        """``released`` is the one state with positive evidence the container is
        gone, so publishing it would put a warning on a genuinely free slot."""
        response = _validator_heartbeats_response(
            rows=[self._heartbeat_row()],
            assignments=[],
            active_work=[],
            confirmation_work=[],
            orphaned_leases=[self._orphan("released")],
            now=datetime.now(UTC),
            active_bench_version=7,
            slot_settings=SLOT_SETTINGS_DEFAULT,
        )

        [entry] = response.validators
        assert entry.orphaned_slots == []

    def test_orphans_land_on_the_validator_that_holds_them(self) -> None:
        response = _validator_heartbeats_response(
            rows=[self._heartbeat_row(), self._heartbeat_row(_OTHER_HOTKEY)],
            assignments=[],
            active_work=[],
            confirmation_work=[],
            orphaned_leases=[self._orphan("still_running")],
            now=datetime.now(UTC),
            active_bench_version=7,
            slot_settings=SLOT_SETTINGS_DEFAULT,
        )

        by_hotkey = {entry.validator_hotkey: entry for entry in response.validators}
        assert len(by_hotkey[_HOTKEY].orphaned_slots) == 1
        assert by_hotkey[_OTHER_HOTKEY].orphaned_slots == []
