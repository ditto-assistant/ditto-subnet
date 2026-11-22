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


def test_live_hosts_at_the_former_8192_cap() -> None:
    """The SN-488 fleet at the old cap: 12 of 40 slots, and what 8 slots needed."""
    envelope = runtime_envelope()
    assert {
        mib: envelope.memory_admitted_slots(mib, 8192)
        for mib in (7934, 15987, 32099, 64085)
    } == {7934: 0, 15987: 1, 32099: 3, 64085: 7}
    assert envelope.host_memory_required_mib(8, 8192) == 66560
    assert envelope.host_memory_required_mib(1, 8192) == 9216


# SN-488 host memory from get_validator_capacity, Nov 22 11:46 AM ET.
_SN488_HOSTS = {
    "5CFtzzb4": 15987,
    "5Cg3DiRf": 7934,
    "5CqJAjSj": 15990,
    "5HEouuDa": 32099,
    "5HT9U1J8": 64085,
}
_CONFIGURED_SLOTS = 8


def test_published_cap_is_the_sn488_fleet_cap() -> None:
    assert runtime_envelope().memory_limit_mib == 863
    assert runtime_envelope().host_memory_reserve_mib == 1024


def test_published_cap_admits_every_configured_slot_on_the_current_fleet() -> None:
    """863 MiB fills all 40 configured slots before any operator slot cap."""
    envelope = runtime_envelope()
    cap = envelope.memory_limit_mib
    admitted = {
        hotkey: min(_CONFIGURED_SLOTS, envelope.memory_admitted_slots(mib, cap))
        for hotkey, mib in _SN488_HOSTS.items()
    }
    assert admitted == dict.fromkeys(_SN488_HOSTS, 8)
    assert sum(admitted.values()) == 40
    # The tightest host has 6 MiB to spare: 8 x 863 + 1024 = 7928 <= 7934.
    assert envelope.host_memory_required_mib(8, cap) == 7928
    assert envelope.host_memory_required_mib(1, cap) == 1887


def test_863_is_the_highest_cap_that_fills_the_tightest_host() -> None:
    envelope = runtime_envelope()
    tightest = min(_SN488_HOSTS.values())
    assert envelope.memory_admitted_slots(tightest, 863) == 8
    assert envelope.memory_admitted_slots(tightest, 864) == 7
