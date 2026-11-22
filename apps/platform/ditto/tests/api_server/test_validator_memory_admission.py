"""The memory admission note operators and validators read."""

from __future__ import annotations

from ditto.api_server.validator_memory_admission import host_memory_admission


def test_live_sn488_hosts_at_the_8192_cap() -> None:
    notes = {
        mib: host_memory_admission(
            memory_total_mib=mib, configured_slots=8, sandbox_memory_limit_mib=8192
        )
        for mib in (7934, 15987, 32099, 64085)
    }
    assert {mib: a.admitted_slots for mib, a in notes.items() if a} == {
        7934: 0,
        15987: 1,
        32099: 3,
        64085: 7,
    }
    assert all(a and a.required_mib == 66560 for a in notes.values())
    zero = notes[7934]
    assert zero is not None
    assert zero.note() == (
        "host memory admits no benchmark slot at the 8,192 MiB harness cap "
        "(7,934 MiB host); needs at least 9,216 MiB for 1 slot and 66,560 MiB "
        "for all 8"
    )


def test_unknown_memory_or_cap_says_nothing() -> None:
    assert (
        host_memory_admission(
            memory_total_mib=None, configured_slots=8, sandbox_memory_limit_mib=8192
        )
        is None
    )
    assert (
        host_memory_admission(
            memory_total_mib=64085, configured_slots=8, sandbox_memory_limit_mib=None
        )
        is None
    )


def test_a_host_admitting_every_configured_slot_has_no_note() -> None:
    admission = host_memory_admission(
        memory_total_mib=64085, configured_slots=4, sandbox_memory_limit_mib=8192
    )
    assert admission is not None
    assert not admission.limited
    assert admission.note() is None


def test_published_cap_admits_33_of_the_fleet_s_40_advertised_slots() -> None:
    """At the published 2048 MiB cap memory admits 7/3/7/8/8 of 8 each."""
    from ditto_screening_protocol.runtime_envelope import runtime_envelope

    cap = runtime_envelope().memory_limit_mib
    assert cap == 2048
    fleet = [
        host_memory_admission(
            memory_total_mib=mib, configured_slots=8, sandbox_memory_limit_mib=cap
        )
        for mib in (15987, 7934, 15990, 32099, 64085)
    ]
    assert all(a is not None for a in fleet)
    assert [min(8, a.admitted_slots) for a in fleet if a] == [7, 3, 7, 8, 8]
    assert sum(min(8, a.admitted_slots) for a in fleet if a) == 33
    assert [a.note() is None for a in fleet if a] == [False, False, False, True, True]
