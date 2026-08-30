"""``ditto attest``: link two hotkeys as the same operator.

Copy screening compares a new submission against everyone else's earlier work.
The same-owner exemption that keeps it from flagging your own history keys on
the wallet, so rotating to a new coldkey/hotkey makes your own v1 look like
someone else's work to the screener. This command mints the cryptographic fix:
a **symmetric owner link** between two hotkeys, signed by both ends. Each end
proves itself with **either** its own hotkey **or** the coldkey that owns it.
Both halves are required, so nobody can link themselves to a hotkey they do
not control.

**Signing moves no TAO.** Each half is an sr25519 signature over a text
message; a coldkey proof decrypts the coldkey to produce that signature and
does not build or submit any extrinsic.

Scope, stated once and repeated in the confirmation and the ``--help``: the
link exempts each hotkey from **plagiarism screening against the linked
hotkey's work only**, including byte-identical and repacked generations, and
clears existing pending copy holds for the direct pair. It does not grant an
additional emission slot.

Walk:

1. Load THIS side's wallet and the OTHER side's wallet
2. Sort the two hotkeys into canonical ``lo``/``hi`` order
3. Mint a fresh nonce + ``issued_at``
4. Sign each half with its own wallet, using its chosen key kind
5. ``--print-only``: print the signed JSON body and stop, submitting nothing
6. Otherwise show the confirmation (skipped by ``--yes``), then
   POST /attestations/owner-link
7. Print the returned attestation_id to stdout; progress + hints to stderr

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
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from ditto.api_models import OwnerLinkProof, OwnerLinkRequest
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.attestation import canonical_pair, sign_link_half, signer_address
from ditto.miner_cli.confirm import confirm_attestation
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet

if TYPE_CHECKING:
    from ditto.miner_cli.attestation import KeyKind, Side

logger = logging.getLogger(__name__)

DEFAULT_NETUID = 118

_NO_TRANSFER = (
    "Signing does NOT transfer any TAO: each half is a signature over a text "
    "message, not a transaction, including when a half is proved with a "
    "coldkey."
)

_SCOPE_SUMMARY = (
    "The link exempts each hotkey from plagiarism screening against the "
    "linked hotkey's work ONLY, including byte-identical and repacked "
    "generations, and clears existing pending copy holds for the direct pair. "
    "It does NOT grant an additional "
    "emission slot (one slot per distinct agent, however many keys you hold), "
    "Links are recorded and auditable; an operator can "
    "revoke a link if either key is sold or compromised. The "
    "evidence grade (coldkey-coldkey / mixed / hotkey-hotkey) is reviewer "
    "context and does not change whether the exemption applies."
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
        help="Link two hotkeys as the same operator (both ends sign).",
        description=(
            "Mint and submit an owner-link attestation declaring that two "
            "hotkeys belong to the same operator. Both ends sign, so both "
            "wallets must be on this machine, and each end may prove itself "
            "with its own hotkey or with the coldkey that owns it. "
            + _NO_TRANSFER
            + " "
            + _SCOPE_SUMMARY
        ),
        parents=parents or [],
    )
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
        help=(
            "Coldkey wallet name holding THIS side's hotkey. Required (flag "
            "or WALLET_NAME env). Matches the bittensor SDK's --wallet.name; "
            "--coldkey is a shorter alias."
        ),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
        help=(
            "Hotkey name within THIS side's coldkey wallet. Required (flag or "
            "HOTKEY_NAME env). Matches the bittensor SDK's --wallet.hotkey; "
            "--hotkey is a shorter alias."
        ),
    )
    parser.add_argument(
        "--other-wallet.name",
        "--other-coldkey",
        dest="other_coldkey_name",
        default=os.environ.get("OTHER_WALLET_NAME"),
        help=(
            "Coldkey wallet name holding the OTHER side's hotkey. Required "
            "(flag or OTHER_WALLET_NAME env). Flag aliases: "
            "--other-wallet.name / --other-coldkey."
        ),
    )
    parser.add_argument(
        "--other-wallet.hotkey",
        "--other-hotkey-name",
        dest="other_hotkey_name",
        default=os.environ.get("OTHER_HOTKEY_NAME"),
        help=(
            "Hotkey name within the OTHER side's coldkey wallet. Required "
            "(flag or OTHER_HOTKEY_NAME env). Flag aliases: "
            "--other-wallet.hotkey / --other-hotkey-name."
        ),
    )
    parser.add_argument(
        "--key-kind",
        dest="key_kind",
        choices=["hotkey", "coldkey"],
        default="hotkey",
        help=(
            "How THIS side proves itself. 'hotkey' signs with the hotkey "
            "being linked. 'coldkey' signs with the coldkey that owns it, "
            "which is the recovery path when that hotkey's key is gone; it "
            "requires that hotkey to have a payment record on the platform "
            "binding it to the coldkey. Signing with a coldkey does NOT move "
            "funds. Defaults to hotkey."
        ),
    )
    parser.add_argument(
        "--other-key-kind",
        dest="other_key_kind",
        choices=["hotkey", "coldkey"],
        default="hotkey",
        help=(
            "How the OTHER side proves itself. Same choices and same "
            "trade-off as --key-kind. Defaults to hotkey."
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
            "Sign both halves, print the JSON request body to stdout, and "
            "submit nothing. Both wallets must still be available on this "
            "machine. Use it when the machine holding both keys should not "
            "talk to the platform: produce the body there, move it, and POST "
            "it yourself. The body expires, so submit it the same day."
        ),
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the attest subcommand and return an exit code."""
    if not args.coldkey_name or not args.hotkey_name:
        print(
            "error: --wallet.name and --wallet.hotkey are required "
            "(or set WALLET_NAME / HOTKEY_NAME).",
            file=sys.stderr,
        )
        return 1
    if not args.other_coldkey_name or not args.other_hotkey_name:
        print(
            "error: --other-wallet.name and --other-wallet.hotkey are "
            "required (or set OTHER_WALLET_NAME / OTHER_HOTKEY_NAME).",
            file=sys.stderr,
        )
        return 1

    network = resolve_network(args.network)

    try:
        return _run_attest(args, network_api_url=network.api_url)
    except MinerCliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _run_attest(
    args: argparse.Namespace, *, network_api_url: str, emit_result: bool = True
) -> int:
    # The link is symmetric, so "this" and "other"
    # name only which flags carried them; neither side is privileged.
    this_handle, this_wallet = load_wallet(
        coldkey_name=args.coldkey_name, hotkey_name=args.hotkey_name
    )
    other_handle, other_wallet = load_wallet(
        coldkey_name=args.other_coldkey_name, hotkey_name=args.other_hotkey_name
    )

    if this_handle.hotkey_ss58 == other_handle.hotkey_ss58:
        raise MinerCliError(
            "both wallets resolve to the same hotkey "
            f"({this_handle.hotkey_ss58}); a hotkey cannot be linked to itself"
        )

    # Canonical order decides which side each wallet signs for. The
    # server sorts the same way and binds each proof to the side its hotkey
    # landed on, so a mislabelled half simply fails to verify.
    hotkey_lo, hotkey_hi = canonical_pair(
        this_handle.hotkey_ss58, other_handle.hotkey_ss58
    )
    this_side: Side = "lo" if this_handle.hotkey_ss58 == hotkey_lo else "hi"
    other_side: Side = "hi" if this_side == "lo" else "lo"
    this_key_kind = cast("KeyKind", args.key_kind)
    other_key_kind = cast("KeyKind", args.other_key_kind)

    # Both halves must bind the identical
    # nonce and issued_at or the platform rejects the pair.
    nonce = uuid4()
    issued_at = datetime.now(UTC)

    # Sign each half with its own wallet and its own key kind. No
    # extrinsic is built here; a coldkey kind only decrypts a key to sign.
    this_signature = sign_link_half(
        live_wallet=this_wallet,
        netuid=args.netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        nonce=nonce,
        issued_at=issued_at,
        side=this_side,
        key_kind=this_key_kind,
    )
    other_signature = sign_link_half(
        live_wallet=other_wallet,
        netuid=args.netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        nonce=nonce,
        issued_at=issued_at,
        side=other_side,
        key_kind=other_key_kind,
    )
    this_proof = OwnerLinkProof(
        key_kind=this_key_kind,
        signer=signer_address(live_wallet=this_wallet, key_kind=this_key_kind),
        signature=this_signature,
    )
    other_proof = OwnerLinkProof(
        key_kind=other_key_kind,
        signer=signer_address(live_wallet=other_wallet, key_kind=other_key_kind),
        signature=other_signature,
    )
    body = OwnerLinkRequest(
        netuid=args.netuid,
        hotkey_a=this_handle.hotkey_ss58,
        hotkey_b=other_handle.hotkey_ss58,
        nonce=nonce,
        issued_at=issued_at,
        proof_a=this_proof,
        proof_b=other_proof,
    )

    # --print-only stops here. Nothing is sent, so no confirmation is
    # asked for: the only side effect is bytes on stdout.
    if args.print_only:
        print(json.dumps(body.model_dump(mode="json"), indent=2))
        print(
            "\nprinted only; nothing was submitted.\n"
            "POST this body to /api/v1/attestations/owner-link from a machine "
            "that can reach the platform.\n" + _NO_TRANSFER + "\n" + _SCOPE_SUMMARY,
            file=sys.stderr,
        )
        return 0

    lo_proof, hi_proof = (
        (this_proof, other_proof) if this_side == "lo" else (other_proof, this_proof)
    )
    confirm_attestation(
        netuid=args.netuid,
        hotkey_lo=hotkey_lo,
        hotkey_hi=hotkey_hi,
        lo_key_kind=lo_proof.key_kind,
        hi_key_kind=hi_proof.key_kind,
        lo_signer=lo_proof.signer,
        hi_signer=hi_proof.signer,
        skip=args.yes,
    )

    print("submitting attestation...", file=sys.stderr)
    with ApiClient(base_url=network_api_url) as client:
        result = client.post_owner_link(body)

    if emit_result:
        print(result.attestation_id)
        print(
            f"\nowner link recorded: {result.hotkey_lo} <-> {result.hotkey_hi}\n"
            f"evidence grade: {result.evidence_grade} (reviewer context only)\n"
            f"scope: {result.scope}\n"
            f"grants an additional emission slot: "
            f"{result.grants_additional_emission_slot}\n" + _SCOPE_SUMMARY,
            file=sys.stderr,
        )
    else:
        print(
            f"owner link recorded before upload: {result.hotkey_lo} <-> "
            f"{result.hotkey_hi} ({result.evidence_grade})",
            file=sys.stderr,
        )
    return 0
