"""Exact gamma amounts: integer units on chain, decimal USD strings on the wire.

On chain one gamma is ``10**9`` units and one gamma is one US dollar, so one
unit is ``10**-9`` USD. Books keep integer units; only responses carry decimal
strings (requirement 3.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

UNITS_PER_USD = 1_000_000_000
"""Units per gamma, and so per US dollar (1 gamma = 1 USD)."""

MIN_SPEND_UNITS = 100_000_000
"""The chain's smallest spend (0.1 gamma) unless a spend empties the deposit."""

_SCALE = Decimal(UNITS_PER_USD)


def units_to_usd(units: int) -> str:
    """``units`` as an exact USD decimal string with at least two decimals.

    ``5_000_000_000`` is ``"5.00"`` and ``1_234_567_891`` is ``"1.234567891"``.
    """
    if isinstance(units, bool) or not isinstance(units, int):
        raise TypeError("units must be an int")
    if units < 0:
        raise ValueError("units must not be negative")
    whole, frac = divmod(units, UNITS_PER_USD)
    digits = f"{frac:09d}".rstrip("0")
    return f"{whole}.{digits.ljust(2, '0')}"


def usd_to_units(usd: str | Decimal) -> int:
    """A non-negative USD amount as exact units; refuses sub-unit precision."""
    try:
        value = Decimal(usd)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("not a decimal USD amount") from None
    if not value.is_finite() or value < 0:
        raise ValueError("USD amount must be finite and not negative")
    units = value * _SCALE
    if units != units.to_integral_value():
        raise ValueError("USD amount is finer than one unit (1e-9 USD)")
    return int(units)


@dataclass(frozen=True)
class BalanceView:
    """One hotkey's account: what was credited and what was used, in units."""

    hotkey: str
    credited_units: int = 0
    used_units: int = 0

    def __post_init__(self) -> None:
        if self.credited_units < 0 or self.used_units < 0:
            raise ValueError("balances must not be negative")
        if self.used_units > self.credited_units:
            raise ValueError("used must not exceed credited")

    @property
    def remaining_units(self) -> int:
        return self.credited_units - self.used_units

    def to_response(self) -> dict[str, str]:
        """The balance endpoint body (requirement 3.1); zeros for an unknown key."""
        return {
            "hotkey": self.hotkey,
            "credited_usd": units_to_usd(self.credited_units),
            "used_usd": units_to_usd(self.used_units),
            "remaining_usd": units_to_usd(self.remaining_units),
        }
