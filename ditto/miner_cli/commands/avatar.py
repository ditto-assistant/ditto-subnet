"""``ditto avatar``: set or clear a signed miner profile picture."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ditto.api_models.miner_avatar import (
    MinerAvatarClearRequest,
    MinerAvatarSetRequest,
)
from ditto.api_models.name_claim import NameClaimProof
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.confirm import confirm_avatar_action
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.miner_avatar import (
    clear_message,
    set_message,
    sign_payload,
    signer_address,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet

if TYPE_CHECKING:
    from ditto.miner_cli.miner_avatar import KeyKind

DEFAULT_NETUID = 118


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    """Register ``avatar {set,clear}``."""
    parser = subparsers.add_parser(
        "avatar",
        help="Set or clear a miner profile picture.",
        description=(
            "Signed-hotkey profile picture. Upload a PNG, JPEG, or WebP image "
            "and show it on the public dashboard. Signing does not transfer TAO."
        ),
        parents=parents or [],
    )
    avatar_subs = parser.add_subparsers(dest="avatar_command", required=True)
    _add_set(avatar_subs)
    _add_clear(avatar_subs)
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
        help="Subnet the avatar is minted for. Default 118.",
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


def _add_set(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "set",
        help="Upload a signed PNG, JPEG, or WebP profile picture.",
    )
    parser.add_argument(
        "--file",
        dest="file",
        required=True,
        help="Path to a PNG, JPEG, or WebP image (max 512 KiB).",
    )
    _wallet_flags(parser)
    parser.set_defaults(func=run)


def _add_clear(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "clear",
        help="Remove the current profile picture for this hotkey.",
    )
    _wallet_flags(parser)
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Dispatch ``ditto avatar`` subcommands."""
    network = resolve_network(args.network)
    if not args.coldkey_name or not args.hotkey_name:
        raise MinerCliError("wallet name and hotkey are required")

    handle, wallet = load_wallet(
        coldkey_name=args.coldkey_name, hotkey_name=args.hotkey_name
    )
    key_kind: KeyKind = args.key_kind
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    signer = signer_address(live_wallet=wallet, key_kind=key_kind)

    if args.avatar_command == "set":
        path = Path(args.file).expanduser()
        if not path.is_file():
            raise MinerCliError(f"avatar file not found: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        confirm_avatar_action(
            action="set",
            hotkey_ss58=handle.hotkey_ss58,
            detail=str(path),
            skip=args.yes,
        )
        payload = set_message(
            netuid=args.netuid,
            miner_hotkey=handle.hotkey_ss58,
            content_sha256=digest,
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
        body = MinerAvatarSetRequest(
            netuid=args.netuid,
            miner_hotkey=handle.hotkey_ss58,
            nonce=nonce,
            issued_at=issued_at,
            proof=proof,
        )
        if args.print_only:
            print(body.model_dump_json(indent=2))
            return 0
        with ApiClient(base_url=network.api_url) as client:
            result = client.post_miner_avatar(body=body, image=raw, filename=path.name)
        print(result.avatar_url or "")
        print(
            f"avatar set for {result.miner_hotkey}: {result.content_type}",
            file=sys.stderr,
        )
        return 0

    if args.avatar_command == "clear":
        confirm_avatar_action(
            action="clear",
            hotkey_ss58=handle.hotkey_ss58,
            detail=None,
            skip=args.yes,
        )
        payload = clear_message(
            netuid=args.netuid,
            miner_hotkey=handle.hotkey_ss58,
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
        clear_body = MinerAvatarClearRequest(
            netuid=args.netuid,
            miner_hotkey=handle.hotkey_ss58,
            nonce=nonce,
            issued_at=issued_at,
            proof=proof,
        )
        if args.print_only:
            print(clear_body.model_dump_json(indent=2))
            return 0
        with ApiClient(base_url=network.api_url) as client:
            result = client.clear_miner_avatar(clear_body)
        print(result.miner_hotkey)
        print("avatar cleared", file=sys.stderr)
        return 0

    raise MinerCliError(f"unknown avatar command {args.avatar_command!r}")
