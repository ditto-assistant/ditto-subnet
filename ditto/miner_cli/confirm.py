"""Interactive confirmation prompts for the irreversible CLI actions.

Standalone module so each orchestrator can call one symbol and tests
can monkeypatch :func:`builtins.input` cleanly. Mirrors the btcli
default cadence (interactive prompt by default, ``-y`` / ``--yes`` /
``--no-prompt`` to skip), see ``/root/btcli/bittensor_cli/cli.py:287``
for the prior art we're matching.

Two actions prompt: paying the evaluation fee (:func:`confirm_payment`)
and publishing a hotkey-rotation attestation
(:func:`confirm_attestation`). The attestation preview restates the
link's scope in full, because the one thing a miner must not be able to
misread is what the link does and does not grant.
"""

from __future__ import annotations

import logging
import sys
from decimal import Decimal

from ditto.miner_cli.errors import AttestationCancelledError, PaymentCancelledError

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


def confirm_attestation(
    *,
    netuid: int,
    old_hotkey_ss58: str,
    old_coldkey_name: str,
    new_hotkey_ss58: str,
    new_coldkey_name: str,
    skip: bool,
) -> None:
    """Show the rotation-attestation preview + prompt for confirmation.

    The preview spells the scope out rather than summarising it. The link is
    narrow, it is public, and it is permanent until revoked, so a miner who
    reads only this screen must still come away with the correct expectation.

    Args:
        netuid: Subnet the attestation is minted for.
        old_hotkey_ss58: Predecessor hotkey that signs the attestation.
        old_coldkey_name: Wallet coldkey name for the old hotkey (display
            only).
        new_hotkey_ss58: Successor hotkey that signs the acceptance.
        new_coldkey_name: Wallet coldkey name for the new hotkey (display
            only).
        skip: When ``True`` the prompt is bypassed entirely. Used by the
            ``-y`` / ``--yes`` flag for scripted invocations.

    Raises:
        AttestationCancelledError: When the user does not answer ``y`` (any
            other input including blank + EOF declines).
    """
    print()
    print("Hotkey rotation attestation")
    print(f"  Netuid:      {netuid}")
    print(f"  Old hotkey:  {old_hotkey_ss58}  (coldkey: {old_coldkey_name})")
    print(f"  New hotkey:  {new_hotkey_ss58}  (coldkey: {new_coldkey_name})")
    print()
    print("Both wallets sign: the old hotkey attests, the new hotkey accepts.")
    print("What this link does:")
    print("  - It exempts the new hotkey from plagiarism screening against")
    print("    the old hotkey's earlier work ONLY. Nothing else is exempted.")
    print("What it does NOT do:")
    print("  - It does NOT grant an additional emission slot. One slot per")
    print("    distinct agent, no matter how many keys you hold.")
    print("  - It does NOT permit byte-identical or repacked resubmission;")
    print("    those are still held for review.")
    print("The link is recorded, auditable, and revocable.")
    print()

    if skip:
        logger.debug("attestation confirmation bypassed via --yes")
        return

    try:
        response = input("Submit this attestation? [y/N]: ").strip().lower()
    except EOFError as e:
        raise AttestationCancelledError("attestation cancelled: EOF on stdin") from e

    if response != "y":
        raise AttestationCancelledError(
            f"attestation cancelled (response={response!r})"
        )

    print("attestation confirmed", file=sys.stderr)
