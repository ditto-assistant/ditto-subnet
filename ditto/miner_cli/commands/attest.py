"""``ditto attest``: link a rotated hotkey to its predecessor.

Copy screening compares a new submission against everyone else's earlier work.
The same-owner exemption that keeps it from flagging your own history keys on
the wallet, so rotating to a new coldkey/hotkey makes your own v1 look like
someone else's work to the screener. This command mints the cryptographic fix:
the **old** hotkey signs a statement that the **new** hotkey continues it, and
the **new** hotkey counter-signs to accept. Both halves are required, so nobody
can name a hotkey they do not control as their successor.

Scope, stated once and repeated in the confirmation and the ``--help``: the
link exempts the new hotkey from **plagiarism screening against the old
hotkey's earlier work only**. It does not grant an additional emission slot,
and it does not permit byte-identical resubmission.

Walk:

1. Load the OLD wallet and the NEW wallet
2. Mint a fresh nonce + ``issued_at``
3. Sign the attestation half with the old hotkey, the acceptance half with
   the new hotkey
4. ``--print-only``: print the signed JSON body and stop, submitting nothing
5. Otherwise show the confirmation (skipped by ``--yes``), then
   POST /attestations/hotkey-rotation
6. Print the returned attestation_id to stdout; progress + hints to stderr

Exit codes:
- 0 success (attestation_id, or the signed body under --print-only, on stdout)
- 1 any :class:`MinerCliError` (wallet missing, same key twice, declined
  confirmation, API rejection)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

from ditto.api_models import HotkeyAttestationRequest
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.attestation import sign_acceptance, sign_attestation
from ditto.miner_cli.confirm import confirm_attestation
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet

logger = logging.getLogger(__name__)

DEFAULT_NETUID = 118

_SCOPE_SUMMARY = (
    "The link exempts the new hotkey from plagiarism screening against the "
    "old hotkey's earlier work ONLY. It does NOT grant an additional emission "
    "slot (one slot per distinct agent, however many keys you hold), and it "
    "does NOT permit byte-identical or repacked resubmission. Links are "
    "recorded, auditable, and revocable."
)


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    """Register the ``attest`` subparser on the top-level argparse layout.

    ``parents`` carries the shared top-level flags (``--network``,
    ``--subtensor.chain_endpoint``, ``--verbose``) so they accept the
    position after the subcommand as well as before it.
    """
    parser = subparsers.add_parser(
        "attest",
        help="Link a rotated hotkey to its predecessor (both keys sign).",
        description=(
            "Mint and submit a hotkey-rotation attestation. The old hotkey "
            "attests that the new hotkey continues it; the new hotkey "
            "counter-signs to accept, so both wallets must be on this "
            "machine. " + _SCOPE_SUMMARY
        ),
        parents=parents or [],
    )
    parser.add_argument(
        "--old-wallet.name",
        "--old-coldkey",
        dest="old_coldkey_name",
        default=os.environ.get("OLD_WALLET_NAME"),
        help=(
            "Coldkey wallet name holding the OLD hotkey (the one that signs "
            "the attestation). Required (flag or OLD_WALLET_NAME env). Flag "
            "aliases: --old-wallet.name / --old-coldkey."
        ),
    )
    parser.add_argument(
        "--old-wallet.hotkey",
        "--old-hotkey-name",
        dest="old_hotkey_name",
        default=os.environ.get("OLD_HOTKEY_NAME"),
        help=(
            "Hotkey name within the OLD coldkey wallet. Required (flag or "
            "OLD_HOTKEY_NAME env). Flag aliases: --old-wallet.hotkey / "
            "--old-hotkey-name."
        ),
    )
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
        help=(
            "Coldkey wallet name holding the NEW hotkey (the one that signs "
            "the acceptance). Required (flag or WALLET_NAME env). Matches the "
            "bittensor SDK's --wallet.name; --coldkey is a shorter alias."
        ),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
        help=(
            "Hotkey name within the NEW coldkey wallet. Required (flag or "
            "HOTKEY_NAME env). Matches the bittensor SDK's --wallet.hotkey; "
            "--hotkey is a shorter alias."
        ),
    )
    parser.add_argument(
        "--netuid",
        type=int,
        default=int(os.environ.get("NETUID", str(DEFAULT_NETUID))),
        help=(
            "Subnet the attestation is minted for. Signed into both payloads, "
            "so an attestation minted for one subnet cannot be replayed onto "
            f"another. Env: NETUID. Defaults to {DEFAULT_NETUID}."
        ),
    )
    parser.add_argument(
        "-y",
        "--yes",
        dest="yes",
        action="store_true",
        help="Skip the interactive confirmation. For scripted use.",
    )
    parser.add_argument(
        "--print-only",
        dest="print_only",
        action="store_true",
        help=(
            "Mint and sign both halves, print the JSON request body to "
            "stdout, and submit nothing. Use this when the old key lives on "
            "a machine that should not talk to the platform: produce the "
            "body there, move it, and POST it yourself. The body expires, so "
            "submit it the same day."
        ),
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the attest subcommand and return an exit code."""
    if not args.old_coldkey_name or not args.old_hotkey_name:
        print(
            "error: --old-wallet.name and --old-wallet.hotkey are required "
            "(or set OLD_WALLET_NAME / OLD_HOTKEY_NAME).",
            file=sys.stderr,
        )
        return 1
    if not args.coldkey_name or not args.hotkey_name:
        print(
            "error: --wallet.name and --wallet.hotkey are required "
            "(or set WALLET_NAME / HOTKEY_NAME).",
            file=sys.stderr,
        )
        return 1

    network = resolve_network(args.network)

    try:
        return _run_attest(args, network_api_url=network.api_url)
    except MinerCliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _run_attest(args: argparse.Namespace, *, network_api_url: str) -> int:
    # Step 1: load both wallets. The old one signs the attestation, the new
    # one signs the acceptance; a rotation attestation needs both on disk.
    old_handle, old_wallet = load_wallet(
        coldkey_name=args.old_coldkey_name, hotkey_name=args.old_hotkey_name
    )
    new_handle, new_wallet = load_wallet(
        coldkey_name=args.coldkey_name, hotkey_name=args.hotkey_name
    )

    if old_handle.hotkey_ss58 == new_handle.hotkey_ss58:
        raise MinerCliError(
            "old and new hotkey resolve to the same address "
            f"({old_handle.hotkey_ss58}); a hotkey cannot succeed itself"
        )

    # Step 2: mint the shared tuple. Both halves must bind the identical
    # nonce and issued_at or the platform rejects the pair.
    nonce = uuid4()
    issued_at = datetime.now(UTC)

    # Step 3: sign each half with its own wallet.
    attestation_signature = sign_attestation(
        live_wallet=old_wallet,
        netuid=args.netuid,
        old_hotkey=old_handle.hotkey_ss58,
        new_hotkey=new_handle.hotkey_ss58,
        nonce=nonce,
        issued_at=issued_at,
    )
    acceptance_signature = sign_acceptance(
        live_wallet=new_wallet,
        netuid=args.netuid,
        old_hotkey=old_handle.hotkey_ss58,
        new_hotkey=new_handle.hotkey_ss58,
        nonce=nonce,
        issued_at=issued_at,
    )
    body = HotkeyAttestationRequest(
        netuid=args.netuid,
        old_hotkey=old_handle.hotkey_ss58,
        new_hotkey=new_handle.hotkey_ss58,
        nonce=nonce,
        issued_at=issued_at,
        attestation_signature=attestation_signature,
        acceptance_signature=acceptance_signature,
    )

    # Step 4: --print-only stops here. Nothing is sent, so no confirmation is
    # asked for: the only side effect is bytes on stdout.
    if args.print_only:
        print(json.dumps(body.model_dump(mode="json"), indent=2))
        print(
            "\nprinted only; nothing was submitted.\n"
            "POST this body to /api/v1/attestations/hotkey-rotation from a "
            "machine that can reach the platform.\n" + _SCOPE_SUMMARY,
            file=sys.stderr,
        )
        return 0

    # Step 5: confirm, then submit.
    confirm_attestation(
        netuid=args.netuid,
        old_hotkey_ss58=old_handle.hotkey_ss58,
        old_coldkey_name=old_handle.coldkey_name,
        new_hotkey_ss58=new_handle.hotkey_ss58,
        new_coldkey_name=new_handle.coldkey_name,
        skip=args.yes,
    )

    print("submitting attestation...", file=sys.stderr)
    with ApiClient(base_url=network_api_url) as client:
        result = client.post_hotkey_attestation(body)

    # Step 6: attestation_id to stdout, everything else to stderr.
    print(result.attestation_id)
    print(
        f"\nattestation recorded: {result.old_hotkey} -> {result.new_hotkey}\n"
        f"scope: {result.scope}\n"
        f"grants an additional emission slot: "
        f"{result.grants_additional_emission_slot}\n" + _SCOPE_SUMMARY,
        file=sys.stderr,
    )
    return 0
