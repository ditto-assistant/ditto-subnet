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


def test_resource_constrained_reports_the_claim_but_offers_nothing() -> None:
    """A constrained host is visibly idle-by-choice, not silently absent.

    It keeps advertising ``configured_slots`` and keeps reporting the lease it
    already holds, so the platform's liveness gate still sees the live run --
    it just has no free slot to give it. That distinction is the whole point:
    an absent slot was indistinguishable from a free one, which is what cost a
    live lease before (#274).
    """
    capacity = BenchmarkCapacity(
        configured_slots=4,
        admission="resource_constrained",
        healthy_slots=[],
        active=[_active("slot-1")],
    )

    assert capacity.free_healthy_slots == ()
    assert capacity.configured_slots == 4
    assert [slot.slot_id for slot in capacity.active] == ["slot-1"]


def test_resource_constrained_cannot_also_advertise_healthy_slots() -> None:
    with pytest.raises(ValidationError):
        BenchmarkCapacity(
            configured_slots=2,
            admission="resource_constrained",
            healthy_slots=["slot-0"],
        )


def test_an_older_validators_capacity_token_is_unchanged() -> None:
    """Adding an admission value adds no key, so no signature moves.

    ``admission`` was already part of every v10+ signed token. A validator that
    has never heard of the new value signs exactly the bytes it always did, so
    the fleet's proto 6 and proto 15 members keep verifying.
    """
    legacy = BenchmarkCapacity(configured_slots=2, healthy_slots=["slot-0", "slot-1"])

    assert benchmark_capacity_signing_token(legacy) == (
        '94:{"active":[],"admission":"accepting","configured_slots":2,'
        '"healthy_slots":["slot-0","slot-1"]}'
    )
