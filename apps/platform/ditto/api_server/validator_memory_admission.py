"""How many benchmark slots a validator host's memory admits, and the fix path.

A validator offers at most ``(host memory - reserve) // harness cap`` slots
(:func:`ditto_screening_protocol.runtime_envelope.admitted_slots`), so a host
advertising eight configured slots may run far fewer. That is the overcommit
guard working, but nothing said so: the fleet read "8 slots, 1 healthy" and a
host that could fit no harness at all read ``healthy``. This module turns the
host's announced memory and the live sandbox cap into the admitted count and
the host memory each fix would need, for every operator and validator view.
"""

from __future__ import annotations

from dataclasses import dataclass

from ditto_screening_protocol.runtime_envelope import runtime_envelope


@dataclass(frozen=True)
class HostMemoryAdmission:
    """One host's memory admission at the live harness cap."""

    host_memory_mib: int
    sandbox_memory_limit_mib: int
    configured_slots: int
    admitted_slots: int
    required_mib: int
    """Least host memory that admits every configured slot at this cap."""
    one_slot_required_mib: int

    @property
    def limited(self) -> bool:
        return self.admitted_slots < self.configured_slots

    def note(self) -> str | None:
        """The operator-facing reason and fix path, or ``None`` when unlimited."""
        if not self.limited:
            return None
        cap = f"{self.sandbox_memory_limit_mib:,} MiB harness cap"
        host = f"{self.host_memory_mib:,} MiB host"
        if self.admitted_slots == 0:
            return (
                f"host memory admits no benchmark slot at the {cap} ({host}); "
                f"needs at least {self.one_slot_required_mib:,} MiB for 1 slot "
                f"and {self.required_mib:,} MiB for all {self.configured_slots}"
            )
        return (
            f"host memory admits {self.admitted_slots} of "
            f"{self.configured_slots} slots at the {cap} ({host}); needs at "
            f"least {self.required_mib:,} MiB for all {self.configured_slots}"
        )


def host_memory_admission(
    *,
    memory_total_mib: int | None,
    configured_slots: int,
    sandbox_memory_limit_mib: int | None,
) -> HostMemoryAdmission | None:
    """Admission for one host, or ``None`` when its memory or the cap is unknown.

    Uses the published reserve and one-slot headroom. A validator configured
    with its own reserve may admit slightly differently; its own log says so.
    """
    if memory_total_mib is None or not sandbox_memory_limit_mib:
        return None
    envelope = runtime_envelope()
    return HostMemoryAdmission(
        host_memory_mib=memory_total_mib,
        sandbox_memory_limit_mib=sandbox_memory_limit_mib,
        configured_slots=configured_slots,
        admitted_slots=envelope.memory_admitted_slots(
            memory_total_mib, sandbox_memory_limit_mib
        ),
        required_mib=envelope.host_memory_required_mib(
            configured_slots, sandbox_memory_limit_mib
        ),
        one_slot_required_mib=envelope.host_memory_required_mib(
            1, sandbox_memory_limit_mib
        ),
    )


__all__ = ["HostMemoryAdmission", "host_memory_admission"]
