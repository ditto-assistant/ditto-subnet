"""``ditto name``: claim, endorse, list, or withdraw a public miner handle.

A miner who has been using a name like ``Jupiter`` can sign a claim saying
other families cannot upload as ``Jupiter-ditto-v10``. Entrenched miners
(those with a scored agent family at least 7 days old) endorse the claim.
Once three distinct families have signed, the stem is reserved.

Signing moves no TAO.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from ditto.api_models.name_claim import (
    NameClaimProof,
    NameClaimRequest,
    NameEndorseRequest,
    NameWithdrawRequest,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.confirm import confirm_name_action
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.name_claim import (
    claim_message,
    endorse_message,
    sign_payload,
    signer_address,
    withdraw_message,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

if TYPE_CHECKING:
    from ditto.miner_cli.name_claim import KeyKind

DEFAULT_NETUID = 118


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    """Register ``name {claim,endorse,list,withdraw}``."""
    parser = subparsers.add_parser(
        "name",
        help="Claim or endorse a public miner handle.",
        description=(
            "Signed-hotkey handle reservation. Claim a name you already use, "
            "ask entrenched miners with agent families to endorse it, and "
            "block later copycats from uploading under that handle. Signing "
            "does not transfer TAO."
        ),
        parents=parents or [],
    )
    name_subs = parser.add_subparsers(dest="name_command", required=True)
    _add_claim(name_subs)
    _add_endorse(name_subs)
    _add_list(name_subs)
    _add_withdraw(name_subs)
    return parser


def _wallet_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
        help="Coldkey wallet name. Flag or WALLET_NAME env.",
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
        help="Hotkey name. Flag or HOTKEY_NAME env.",
    )
    parser.add_argument(
        "--key-kind",
        dest="key_kind",
        choices=["hotkey", "coldkey"],
        default="hotkey",
        help="Which key signs. Default hotkey.",
    )
    parser.add_argument(
        "--netuid",
        dest="netuid",
        type=int,
        default=int(os.environ.get("NETUID", str(DEFAULT_NETUID))),
        help="Subnet the claim is minted for. Default 118.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt.",
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="Print the signed JSON body and do not submit it.",
    )


def _add_claim(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "claim",
        help="Sign a reservation for a handle you already use.",
    )
    parser.add_argument(
        "--name",
        dest="name",
        required=True,
        help="Display name or stem to reserve (e.g. Jupiter or Jupiter-ditto-v10).",
    )
    _wallet_flags(parser)
    parser.set_defaults(func=run)


def _add_endorse(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "endorse",
        help="Endorse someone else's handle claim (entrenched families only).",
    )
    parser.add_argument("--claim-id", dest="claim_id", required=True)
    parser.add_argument(
        "--stem",
        dest="name_stem",
        required=True,
        help="Normalized stem on the claim you are endorsing.",
    )
    _wallet_flags(parser)
    parser.set_defaults(func=run)


def _add_list(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("list", help="List live handle claims.")
    parser.set_defaults(func=run)


def _add_withdraw(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "withdraw",
        help="Release a handle you previously claimed.",
    )
    parser.add_argument("--claim-id", dest="claim_id", required=True)
    _wallet_flags(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Dispatch ``ditto name`` subcommands."""
    network = resolve_network(args.network)
    command = args.name_command
    if command == "list":
        with ApiClient(base_url=network.api_url) as client:
            listing = client.list_name_claims()
        print(listing.model_dump_json(indent=2))
        return 0

    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not bool(args.yes),
    )
    handle, wallet = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
    key_kind: KeyKind = args.key_kind
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    signer = signer_address(live_wallet=wallet, key_kind=key_kind)

    if command == "claim":
        stem = _local_stem(args.name)
        confirm_name_action(
            action="claim",
            name=args.name,
            hotkey_ss58=handle.hotkey_ss58,
            skip=args.yes,
        )
        payload = claim_message(
            netuid=args.netuid,
            name_stem=stem,
            claimant_hotkey=handle.hotkey_ss58,
            nonce=nonce,
            issued_at=issued_at,
            key_kind=key_kind,
            signer=signer,
        )
        proof = NameClaimProof(
            key_kind=key_kind,
            signer=signer,
            signature=sign_payload(
                live_wallet=wallet, key_kind=key_kind, payload=payload
            ),
        )
        body = NameClaimRequest(
            netuid=args.netuid,
            name=args.name,
            claimant_hotkey=handle.hotkey_ss58,
            nonce=nonce,
            issued_at=issued_at,
            proof=proof,
        )
        if args.print_only:
            print(body.model_dump_json(indent=2))
            return 0
        with ApiClient(base_url=network.api_url) as client:
            result = client.post_name_claim(body)
        print(result.claim_id)
        print(
            f"claim {result.status} for handle {result.name_stem!r}: "
            f"{result.endorsement_count}/{result.endorsement_threshold} endorsements",
            file=sys.stderr,
        )
        return 0

    if command == "endorse":
        claim_id = UUID(args.claim_id)
        confirm_name_action(
            action="endorse",
            name=args.name_stem,
            hotkey_ss58=handle.hotkey_ss58,
            skip=args.yes,
        )
        payload = endorse_message(
            netuid=args.netuid,
            claim_id=claim_id,
            name_stem=args.name_stem,
            endorser_hotkey=handle.hotkey_ss58,
            nonce=nonce,
            issued_at=issued_at,
            key_kind=key_kind,
            signer=signer,
        )
        proof = NameClaimProof(
            key_kind=key_kind,
            signer=signer,
            signature=sign_payload(
                live_wallet=wallet, key_kind=key_kind, payload=payload
            ),
        )
        endorse_body = NameEndorseRequest(
            netuid=args.netuid,
            name_stem=args.name_stem,
            endorser_hotkey=handle.hotkey_ss58,
            nonce=nonce,
            issued_at=issued_at,
            proof=proof,
        )
        if args.print_only:
            print(endorse_body.model_dump_json(indent=2))
            return 0
        with ApiClient(base_url=network.api_url) as client:
            result = client.post_name_endorsement(claim_id, endorse_body)
        print(result.claim_id)
        print(
            f"claim {result.status}: "
            f"{result.endorsement_count}/{result.endorsement_threshold} endorsements",
            file=sys.stderr,
        )
        return 0

    if command == "withdraw":
        claim_id = UUID(args.claim_id)
        confirm_name_action(
            action="withdraw",
            name=str(claim_id),
            hotkey_ss58=handle.hotkey_ss58,
            skip=args.yes,
        )
        # The server binds the stored stem; the CLI still has to sign one, so
        # fetch the claim first unless this is print-only with no network.
        with ApiClient(base_url=network.api_url) as client:
            existing = client.get_name_claim(claim_id)
            payload = withdraw_message(
                netuid=args.netuid,
                claim_id=claim_id,
                name_stem=existing.name_stem,
                claimant_hotkey=handle.hotkey_ss58,
                nonce=nonce,
                issued_at=issued_at,
                key_kind=key_kind,
                signer=signer,
            )
            proof = NameClaimProof(
                key_kind=key_kind,
                signer=signer,
                signature=sign_payload(
                    live_wallet=wallet, key_kind=key_kind, payload=payload
                ),
            )
            withdraw_body = NameWithdrawRequest(
                netuid=args.netuid,
                claimant_hotkey=handle.hotkey_ss58,
                nonce=nonce,
                issued_at=issued_at,
                proof=proof,
            )
            if args.print_only:
                print(withdraw_body.model_dump_json(indent=2))
                return 0
            result = client.post_name_withdraw(claim_id, withdraw_body)
        print(result.claim_id)
        print(f"claim {result.status} for handle {result.name_stem!r}", file=sys.stderr)
        return 0

    raise MinerCliError(f"unknown name command {command!r}")


def _local_stem(name: str) -> str:
    """Copy of the platform stem rule so the CLI does not import api_server."""
    import re
    import unicodedata

    filler = {"ditto", "sn118", "subnet", "miner", "agent"}
    folded = unicodedata.normalize("NFKC", name).strip().lower()
    folded = re.sub(r"[_.\s]+", "-", folded)
    folded = re.sub(r"[^a-z0-9-]+", "", folded)
    folded = re.sub(r"-{2,}", "-", folded).strip("-")
    tokens = [
        token
        for token in folded.split("-")
        if token and token not in filler and re.fullmatch(r"v?\d+", token) is None
    ]
    stem = "-".join(tokens)
    if not (3 <= len(stem) <= 64):
        raise MinerCliError(
            f"name {name!r} does not yield a reservable handle after "
            "stripping versions and filler"
        )
    return stem
