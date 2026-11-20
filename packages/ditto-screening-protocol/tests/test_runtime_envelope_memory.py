"""Host memory admission and its inverse, the fix path operators are told."""

from __future__ import annotations

import pytest

from ditto_screening_protocol.runtime_envelope import (
    admitted_slots,
    required_host_memory_mib,
    runtime_envelope,
)

_RESERVE = 1024
_HEADROOM = 1024


@pytest.mark.parametrize("cap", [512, 6144, 7680, 8192, 12288])
@pytest.mark.parametrize("slots", range(1, 9))
def test_required_memory_is_the_exact_inverse_of_admission(cap: int, slots: int) -> None:
    required = required_host_memory_mib(
        slots, cap, reserve_mib=_RESERVE, single_slot_headroom_mib=_HEADROOM
    )
    kwargs = {"reserve_mib": _RESERVE, "single_slot_headroom_mib": _HEADROOM}
    assert admitted_slots(required, cap, **kwargs) >= slots
    assert admitted_slots(required - 1, cap, **kwargs) < slots


def test_one_slot_floor_needs_less_than_the_reserve_rule() -> None:
    # A host above one cap plus headroom keeps one slot even under the reserve.
    assert (
        required_host_memory_mib(
            1, 8192, reserve_mib=4096, single_slot_headroom_mib=1024
        )
        == 9216
    )
    assert required_host_memory_mib(
        0, 8192, reserve_mib=_RESERVE, single_slot_headroom_mib=_HEADROOM
    ) == 0


def test_live_hosts_at_the_8192_cap() -> None:
    """The SN-488 fleet: what each announced host admits, and what 8 slots need."""
    envelope = runtime_envelope()
    assert {
        mib: envelope.memory_admitted_slots(mib, 8192)
        for mib in (7934, 15987, 32099, 64085)
    } == {7934: 0, 15987: 1, 32099: 3, 64085: 7}
    assert envelope.host_memory_required_mib(8, 8192) == 66560
    assert envelope.host_memory_required_mib(1, 8192) == 9216
