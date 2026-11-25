"""The books: credit what ``GammaSpent`` grew by, exactly once, and charge it.

Two records (the standard's "Books"):

* **totals** keyed by ``(netuid, registered_counter, service, user hotkey)``: the
  last ``GammaSpent`` total credited, in units;
* **accounts** keyed by user hotkey: ``credited`` and ``used``, in units.

Crediting compares the observed total with the stored one and credits only the
growth, in one atomic step. A repeated, older or concurrent read credits
nothing. Keeping the counter in the key means a dissolved and re-registered
netuid starts again from zero.

:class:`InMemoryChainGammaLedger` is the reference the Postgres ledger must
match; the tests in ``ditto/tests/chain_gamma`` are written against the
protocol so they can run against both.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from ditto.chain_gamma.amounts import BalanceView


@dataclass(frozen=True)
class TotalKey:
    netuid: int
    registered_counter: int
    service_hotkey: str
    user_hotkey: str


@dataclass(frozen=True)
class TotalObservation:
    """``GammaSpent(netuid, service, user)`` read at a finalized block."""

    key: TotalKey
    total_units: int
    block_number: int
    block_hash: str

    def __post_init__(self) -> None:
        if self.total_units < 0 or self.key.registered_counter < 0:
            raise ValueError("totals and counters are unsigned")


class ChargeState(Enum):
    RESERVED = "reserved"
    SETTLED = "settled"


class DuplicateCharge(ValueError):
    """A charge id was reused; ids come from the unique ``(hotkey, nonce)``."""


class ChainGammaLedger(Protocol):
    async def observe_total(self, observation: TotalObservation) -> int:
        """Credit ``total - stored`` when positive; return the units credited."""
        ...

    async def try_charge(self, charge_id: str, hotkey: str, units: int) -> bool:
        """Reserve ``units`` iff ``credited - used >= units``, atomically."""
        ...

    async def settle(self, charge_id: str, actual_units: int) -> int:
        """Lower a reservation to ``actual_units``; return the units returned."""
        ...

    async def balance(self, hotkey: str) -> BalanceView:
        """The account; zeros for an unknown hotkey (requirement 3.3)."""
        ...


@dataclass
class _Charge:
    hotkey: str
    reserved_units: int
    state: ChargeState = ChargeState.RESERVED
    settled_units: int | None = None


class InMemoryChainGammaLedger:
    """Reference semantics; one lock stands in for one database transaction."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._totals: dict[TotalKey, int] = {}
        self._credited: dict[str, int] = {}
        self._used: dict[str, int] = {}
        self._charges: dict[str, _Charge] = {}

    async def observe_total(self, observation: TotalObservation) -> int:
        async with self._lock:
            stored = self._totals.get(observation.key, 0)
            if observation.total_units <= stored:
                return 0
            delta = observation.total_units - stored
            self._totals[observation.key] = observation.total_units
            hotkey = observation.key.user_hotkey
            self._credited[hotkey] = self._credited.get(hotkey, 0) + delta
            return delta

    async def try_charge(self, charge_id: str, hotkey: str, units: int) -> bool:
        if units <= 0:
            raise ValueError("a charge must be positive")
        async with self._lock:
            if charge_id in self._charges:
                raise DuplicateCharge(charge_id)
            credited = self._credited.get(hotkey, 0)
            used = self._used.get(hotkey, 0)
            if credited - used < units:
                return False
            self._used[hotkey] = used + units
            self._charges[charge_id] = _Charge(hotkey=hotkey, reserved_units=units)
            return True

    async def settle(self, charge_id: str, actual_units: int) -> int:
        async with self._lock:
            charge = self._charges.get(charge_id)
            if charge is None:
                raise KeyError(charge_id)
            if charge.state is ChargeState.SETTLED:
                if charge.settled_units == actual_units:
                    return 0
                raise ValueError("charge already settled at a different amount")
            if not 0 <= actual_units <= charge.reserved_units:
                raise ValueError("actual cost must be within the reservation")
            refund = charge.reserved_units - actual_units
            self._used[charge.hotkey] -= refund
            charge.state = ChargeState.SETTLED
            charge.settled_units = actual_units
            return refund

    async def balance(self, hotkey: str) -> BalanceView:
        async with self._lock:
            return BalanceView(
                hotkey=hotkey,
                credited_units=self._credited.get(hotkey, 0),
                used_units=self._used.get(hotkey, 0),
            )
