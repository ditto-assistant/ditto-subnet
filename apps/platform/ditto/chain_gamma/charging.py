"""Verify, then charge, then refuse cleanly at zero (requirements 4 and 5).

For each request to a paid endpoint:

1. verify btauth/1 (401 on failure);
2. charge the price in one conditional update;
3. if nothing was charged, credit this hotkey at the finalized head and retry;
4. if still nothing was charged, answer ``402 {"error": "insufficient_balance"}``
   and do no work;
5. otherwise do the work. A price known only afterwards is reserved as an upper
   bound first and settled down with ``ledger.settle``.

The 402 body is exact, so a route must return it directly rather than through
the Platform error envelope. ``accepting=False`` is the runtime kill switch:
paid work is refused with 503 before any signature or ledger work.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass

from ditto.chain_gamma.btauth import (
    AuthError,
    NonceStore,
    ProtectedSenderCheck,
    VerifiedCaller,
    verify_request,
)
from ditto.chain_gamma.ledger import ChainGammaLedger

INSUFFICIENT_BALANCE = {"error": "insufficient_balance"}
SERVICE_PAUSED = {"error": "service_paused"}

CreditHotkey = Callable[[str], Awaitable[int]]
"""Credit one hotkey from the finalized head; returns the units credited."""


@dataclass(frozen=True)
class ChargeOutcome:
    """What the route answers. ``charge_id`` is set only when work may proceed."""

    status: int
    body: Mapping[str, str] | None = None
    caller: VerifiedCaller | None = None
    charge_id: str | None = None

    @property
    def may_proceed(self) -> bool:
        return self.charge_id is not None


def charge_id_for(caller: VerifiedCaller) -> str:
    """Unique per accepted request, because ``(hotkey, nonce)`` never repeats."""
    return f"{caller.hotkey}:{caller.nonce_ns}"


async def authorize_and_charge(
    *,
    headers: Mapping[str, str],
    body: bytes,
    method: str,
    target: str,
    price_units: int,
    receivers: Collection[str],
    nonce_store: NonceStore,
    is_protected_sender: ProtectedSenderCheck,
    ledger: ChainGammaLedger,
    credit_hotkey: CreditHotkey,
    accepting: bool = True,
    now_ns: int | None = None,
) -> ChargeOutcome:
    if price_units <= 0:
        raise ValueError("a paid endpoint needs a positive price")
    if not accepting:
        return ChargeOutcome(status=503, body=SERVICE_PAUSED)
    try:
        caller = await verify_request(
            headers,
            body,
            method=method,
            target=target,
            receivers=receivers,
            nonce_store=nonce_store,
            is_protected_sender=is_protected_sender,
            now_ns=now_ns,
        )
    except AuthError as exc:
        return ChargeOutcome(
            status=exc.http_status, body={"error": exc.code, "detail": str(exc)}
        )
    charge_id = charge_id_for(caller)
    if await ledger.try_charge(charge_id, caller.hotkey, price_units):
        return ChargeOutcome(status=200, caller=caller, charge_id=charge_id)
    # Charge again even if this read credited nothing: the periodic scan may
    # have credited the hotkey between the first attempt and now.
    await credit_hotkey(caller.hotkey)
    if await ledger.try_charge(charge_id, caller.hotkey, price_units):
        return ChargeOutcome(status=200, caller=caller, charge_id=charge_id)
    return ChargeOutcome(status=402, body=INSUFFICIENT_BALANCE, caller=caller)
