"""Interactive confirmation prompts for the irreversible CLI actions.

Standalone module so each orchestrator can call one symbol and tests
can monkeypatch :func:`builtins.input` cleanly. Mirrors the btcli
default cadence (interactive prompt by default, ``-y`` / ``--yes`` /
``--no-prompt`` to skip), see ``/root/btcli/bittensor_cli/cli.py:287``
for the prior art we're matching.

Two actions prompt: paying the evaluation fee (:func:`confirm_payment`)
and publishing an owner-link attestation (:func:`confirm_attestation`).
The attestation preview restates the link's scope in full, because the
one thing a miner must not be able to misread is what the link does and
does not grant. It also says outright that signing moves no TAO, since a
half may be proved with the coldkey and that is the key which normally
does move funds.
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
    hotkey_lo: str,
    hotkey_hi: str,
    lo_key_kind: str,
    hi_key_kind: str,
    lo_signer: str,
    hi_signer: str,
    skip: bool,
) -> None:
    """Show the owner-link preview + prompt for confirmation.

    The preview spells the scope out rather than summarising it. The link is
    narrow, it is public, and it is permanent until revoked, so a miner who
    reads only this screen must still come away with the correct expectation.
    It leads with "no TAO moves" because one or both halves may be proved by a
    coldkey, and a coldkey signature must never be mistaken for a transfer.

    Args:
        netuid: Subnet the attestation is minted for.
        hotkey_lo: Lower hotkey of the canonically-ordered pair.
        hotkey_hi: Higher hotkey of the canonically-ordered pair.
        lo_key_kind: ``hotkey`` or ``coldkey`` -- how the lo half is proved.
        hi_key_kind: ``hotkey`` or ``coldkey`` -- how the hi half is proved.
        lo_signer: SS58 that signs the lo half.
        hi_signer: SS58 that signs the hi half.
        skip: When ``True`` the prompt is bypassed entirely. Used by the
            ``-y`` / ``--yes`` flag for scripted invocations.

    Raises:
        AttestationCancelledError: When the user does not answer ``y`` (any
            other input including blank + EOF declines).
    """
    print()
    print("Owner-link attestation")
    print(f"  Netuid:     {netuid}")
    print(f"  Hotkey lo:  {hotkey_lo}")
    print(f"    proved by {lo_key_kind}: {lo_signer}")
    print(f"  Hotkey hi:  {hotkey_hi}")
    print(f"    proved by {hi_key_kind}: {hi_signer}")
    print()
    print("This declares the two hotkeys are the same operator. Both ends sign;")
    print("each end may prove itself with its own hotkey or with the coldkey")
    print("that owns it.")
    print()
    print("Signing does NOT transfer any TAO. Each signature covers a text")
    print("message and nothing else. No extrinsic is built and none is sent,")
    print("including when a half is proved with a coldkey.")
    print()
    print("What this link does:")
    print("  - It exempts each hotkey from plagiarism screening against the")
    print("    linked hotkey's earlier work ONLY. Nothing else is exempted.")
    print("What it does NOT do:")
    print("  - It does NOT grant an additional emission slot. One slot per")
    print("    distinct agent, no matter how many keys you hold.")
    print("  - It does NOT permit byte-identical or repacked resubmission;")
    print("    those are still held for review.")
    print("The link is recorded, auditable, and revocable.")
    print("The evidence grade (coldkey-coldkey / mixed / hotkey-hotkey) is")
    print("reviewer context; it does not change whether the exemption applies.")
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
