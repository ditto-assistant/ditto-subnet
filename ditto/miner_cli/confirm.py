"""Interactive payment confirmation prompt.

Standalone module so the orchestrator can call one symbol and tests
can monkeypatch :func:`builtins.input` cleanly. Mirrors the btcli
default cadence (interactive prompt by default, ``-y`` / ``--yes`` /
``--no-prompt`` to skip), see ``/root/btcli/bittensor_cli/cli.py:287``
for the prior art we're matching.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal

from ditto.miner_cli.errors import PaymentCancelledError

logger = logging.getLogger(__name__)


def confirm_payment(
    *,
    amount_rao: int,
    dest_address: str,
    hotkey_ss58: str,
    coldkey_name: str,
    skip: bool,
) -> None:
    """Show a payment preview + prompt for confirmation.

    Args:
        amount_rao: Quoted payment amount in rao.
        dest_address: SS58 address that receives the payment.
        hotkey_ss58: Submitting miner's hotkey (for display only; the
            transfer is signed by the coldkey).
        coldkey_name: Wallet coldkey name (for display only).
        skip: When ``True`` the prompt is bypassed entirely. Used by
            the ``-y`` / ``--yes`` flag for scripted invocations.

    Raises:
        PaymentCancelledError: When the user does not answer ``y`` (any
            other input including blank + EOF declines).
    """
    tao = Decimal(amount_rao) / Decimal(1_000_000_000)
    print()
    print("Payment preview")
    print(f"  Amount:  {tao} TAO  ({amount_rao} rao)")
    print(f"  To:      {dest_address}")
    print(f"  Coldkey: {coldkey_name}")
    print(f"  Hotkey:  {hotkey_ss58}")
    print()

    if skip:
        logger.debug("payment confirmation bypassed via --yes")
        return

    try:
        response = input("Confirm payment? [y/N]: ").strip().lower()
    except EOFError as e:
        raise PaymentCancelledError("payment cancelled: EOF on stdin") from e

    if response != "y":
        raise PaymentCancelledError(f"payment cancelled (response={response!r})")

    print("payment confirmed", file=sys.stderr)
