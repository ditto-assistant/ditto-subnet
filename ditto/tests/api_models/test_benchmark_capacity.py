from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_models.benchmark_capacity import (
    ActiveBenchmarkSlot,
    BenchmarkCapacity,
    benchmark_capacity_signing_token,
)
from ditto.api_models.benchmark_progress import BenchmarkProgress


def _active(slot_id: str) -> ActiveBenchmarkSlot:
    return ActiveBenchmarkSlot(
        slot_id=slot_id,
        agent_id=uuid4(),
        bench_version=5,
        progress=BenchmarkProgress(
            stage="running_benchmark",
            completed=2,
            total=10,
            ticket_deadline=datetime.now(UTC) + timedelta(hours=1),
        ),
    )


def test_capacity_one_is_the_backward_compatible_default() -> None:
    capacity = BenchmarkCapacity()
    assert capacity.configured_slots == 1
    assert capacity.free_healthy_slots == ("slot-0",)
    assert benchmark_capacity_signing_token(capacity).endswith(
        ',"healthy_slots":["slot-0"]}'
    )


def test_two_distinct_active_slots_leave_no_free_capacity() -> None:
    capacity = BenchmarkCapacity(
        configured_slots=2,
        healthy_slots=["slot-0", "slot-1"],
        active=[_active("slot-0"), _active("slot-1")],
    )
    assert capacity.free_healthy_slots == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"configured_slots": 1, "healthy_slots": ["slot-0", "slot-0"]},
        {
            "configured_slots": 2,
            "healthy_slots": ["slot-0"],
            "active": [_active("slot-1"), _active("slot-1")],
        },
        {"configured_slots": 1, "healthy_slots": ["slot-1"]},
        {
            "configured_slots": 2,
            "admission": "draining",
            "healthy_slots": ["slot-0"],
        },
    ],
)
def test_invalid_or_unsafe_capacity_is_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BenchmarkCapacity.model_validate(payload)


def test_draining_can_keep_active_progress_but_has_no_free_slots() -> None:
    capacity = BenchmarkCapacity(
        configured_slots=2,
        admission="draining",
        healthy_slots=[],
        active=[_active("slot-1")],
    )
    assert capacity.free_healthy_slots == ()
