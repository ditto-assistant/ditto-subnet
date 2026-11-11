"""Usage ledger for analysis only. The pilot pays nothing.

A unit is one ``request_id`` whose miner vector Ditto accepted. A replay that
is accepted again is a duplicate: it is counted in ``duplicates_suppressed``
and never earns a second unit. There is no payout math here or anywhere else
in the pilot.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from ..contract import Outcome


class UsageLedger:
    """Accepted-unit accounting keyed by scoped request id."""

    def __init__(self) -> None:
        self._accepted: set[str] = set()
        self._duplicates = 0
        self._rejected = 0
        self._by_outcome: Counter[str] = Counter()
        self._history: dict[str, list[str]] = {}

    def has_accepted(self, request_id: str) -> bool:
        return request_id in self._accepted

    def history(self, request_id: str) -> tuple[str, ...]:
        """Every outcome recorded for ``request_id``, in order."""
        return tuple(self._history.get(request_id, ()))

    @property
    def accepted_units(self) -> int:
        return len(self._accepted)

    @property
    def duplicates_suppressed(self) -> int:
        return self._duplicates

    def accept(self, request_id: str) -> str:
        """Record a validated miner vector. Returns ``Outcome.OK`` for the first
        acceptance of ``request_id`` and ``Outcome.DUPLICATE`` afterwards."""
        if request_id in self._accepted:
            self._duplicates += 1
            outcome = Outcome.DUPLICATE
        else:
            self._accepted.add(request_id)
            outcome = Outcome.OK
        self._note(request_id, outcome)
        return outcome

    def reject(self, request_id: str, outcome: str) -> None:
        """Record a request whose miner vector was not used (any outcome other
        than ``ok`` / ``duplicate``). It never earns a unit."""
        if outcome in (Outcome.OK, Outcome.DUPLICATE):
            raise ValueError(f"{outcome!r} is an acceptance, use accept()")
        self._rejected += 1
        self._note(request_id, outcome)

    def _note(self, request_id: str, outcome: str) -> None:
        self._by_outcome[outcome] += 1
        self._history.setdefault(request_id, []).append(outcome)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_units": self.accepted_units,
            "duplicates_suppressed": self._duplicates,
            "rejected": self._rejected,
            "by_outcome": dict(sorted(self._by_outcome.items())),
            "payouts": "none",
        }
