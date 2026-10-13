"""``ditto login``: approve a dashboard/MCP device grant with a hotkey signature."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from ditto.api_models.miner_session import (
    MinerDeviceStartRequest,
    MinerLoginApproveRequest,
    MinerLogoutAllRequest,
)
from ditto.api_models.name_claim import NameClaimProof
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.confirm import confirm_login_action, confirm_logout_all
from ditto.miner_cli.errors import LoginRequiredError
from ditto.miner_cli.miner_session import (
    login_message,
    logout_all_message,
    sign_payload,
    signer_address,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.preferences import clear_miner_session, save_miner_session
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

if TYPE_CHECKING:
    from ditto.miner_cli.miner_session import KeyKind

DEFAULT_NETUID = 118
DEFAULT_SCOPES = "read,profile"


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "login",
        help="Sign in to the public miner dashboard or MCP.",
        description=(
            "Approve a dittobench.ai login code by signing it with your hotkey. "
            "Signing does not transfer TAO. The resulting session can update "
            "your profile picture and socials without re-signing."
        ),
        parents=parents or [],
    )
    login_subs = parser.add_subparsers(dest="login_command")
    parser.set_defaults(func=run, login_command="approve")
    parser.add_argument(
        "--code",
        dest="user_code",
        help="ABCD-EFGH code shown on dittobench.ai/#/reviews.",
    )
    parser.add_argument(
        "--hours",
        dest="hours",
        type=int,
        default=24,
        help="Session lifetime when starting a CLI session (1-720). Default 24.",
    )
    parser.add_argument(
        "--scopes",
        dest="scopes",
        default=DEFAULT_SCOPES,
        help="Comma-separated scopes when starting a CLI session.",
    )
    _wallet_flags(parser)
    status = login_subs.add_parser("status", help="Show the saved local session.")
    status.set_defaults(func=run, login_command="status")
    logout = login_subs.add_parser(
        "logout",
        help="Forget the saved local session, or sign out everywhere.",
        description=(
            "With --everywhere, sign a request that revokes every live session "
            "for the hotkey. Wallet flags go before the subcommand: "
            "ditto login --hotkey NAME logout --everywhere."
        ),
    )
    logout.set_defaults(func=run, login_command="logout")
    logout.add_argument(
        "--everywhere",
        action="store_true",
        help="Sign out every session for this hotkey, not just this machine's.",
    )
    logout.add_argument(
        "--all-hotkeys",
        dest="all_hotkeys",
        action="store_true",
        help=(
            "With --everywhere and --key-kind coldkey: also sign out every "
            "other hotkey this coldkey funds."
        ),
    )
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
        help=(
            "Hotkey name. Flag or HOTKEY_NAME env. Omit both wallet flags "
            "to search ~/.bittensor/wallets (fzf if installed)."
        ),
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
        # A string default goes through type=int only when this command is
        # parsed, so a bad NETUID cannot crash --help or other commands;
        # an empty one means the default.
        default=os.environ.get("NETUID") or str(DEFAULT_NETUID),
        help="Subnet the login is minted for. Default 118.",
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
        help="Print the signed approval JSON and submit nothing.",
    )


def run(args: argparse.Namespace) -> int:
    command = getattr(args, "login_command", "approve") or "approve"
    if command == "status":
        return _status(args)
    if command == "logout":
        return _logout(args)
    return _approve(args)


def _status(args: argparse.Namespace) -> int:
    from ditto.miner_cli.preferences import load_miner_session

    network = resolve_network(args.network).name
    saved = load_miner_session(network=network)
    if saved is None:
        print("not signed in", file=sys.stderr)
        return 1
    public = {
        "hotkey": saved.get("hotkey"),
        "scopes": saved.get("scopes"),
        "expires_at": saved.get("expires_at"),
    }
    print(json.dumps(public, indent=2, sort_keys=True))
    return 0


def _logout(args: argparse.Namespace) -> int:
    from ditto.miner_cli.preferences import load_miner_session

    if getattr(args, "everywhere", False):
        return _logout_everywhere(args)
    if getattr(args, "all_hotkeys", False):
        print("--all-hotkeys only applies with --everywhere", file=sys.stderr)
        return 2
    network = resolve_network(args.network)
    saved = load_miner_session(network=network.name)
    token = saved.get("token") if isinstance(saved, dict) else None
    if isinstance(token, str) and token:
        try:
            with ApiClient(base_url=network.api_url) as client:
                client.revoke_miner_session(token)
        except LoginRequiredError:
            # Already expired or revoked server-side (for example from the
            # dashboard); only the local copy is left to clear.
            print("server session was already invalid or expired", file=sys.stderr)
        except Exception as exc:
            print(f"could not revoke server session: {exc}", file=sys.stderr)
            return 1
    clear_miner_session(network=network.name)
    print("signed out", file=sys.stderr)
    return 0


def _logout_everywhere(args: argparse.Namespace) -> int:
    network = resolve_network(args.network)
    key_kind: KeyKind = args.key_kind
    all_hotkeys = bool(getattr(args, "all_hotkeys", False))
    if all_hotkeys and key_kind != "coldkey":
        print("--all-hotkeys needs --key-kind coldkey", file=sys.stderr)
        return 2
    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not bool(args.yes),
    )
    handle, wallet = load_wallet(
        coldkey_name=coldkey_name,
        hotkey_name=hotkey_name,
    )
    signer = signer_address(live_wallet=wallet, key_kind=key_kind)
    nonce = uuid4()
    issued_at = datetime.now(UTC)
    payload = logout_all_message(
        netuid=args.netuid,
        miner_hotkey=handle.hotkey_ss58,
        all_funded_hotkeys=all_hotkeys,
        nonce=nonce,
        issued_at=issued_at,
        key_kind=key_kind,
        signer=signer,
    )
    signature = sign_payload(live_wallet=wallet, key_kind=key_kind, payload=payload)
    body = MinerLogoutAllRequest(
        netuid=args.netuid,
        miner_hotkey=handle.hotkey_ss58,
        all_funded_hotkeys=all_hotkeys,
        nonce=nonce,
        issued_at=issued_at,
        proof=NameClaimProof(
            key_kind=key_kind,
            signer=signer,
            signature=signature,
        ),
    )
    if args.print_only:
        print(body.model_dump_json(indent=2))
        return 0
    confirm_logout_all(
        hotkey_ss58=handle.hotkey_ss58,
        key_kind=key_kind,
        all_hotkeys=all_hotkeys,
        skip=bool(args.yes),
    )
    with ApiClient(base_url=network.api_url) as client:
        result = client.logout_all_miner_sessions(body)
    from ditto.miner_cli.preferences import load_miner_session

    saved = load_miner_session(network=network.name)
    if saved is not None and saved.get("hotkey") in result.miner_hotkeys:
        clear_miner_session(network=network.name)
    noun = "session" if result.revoked == 1 else "sessions"
    print(
        f"signed out {result.revoked} {noun} across "
        f"{len(result.miner_hotkeys)} hotkey(s)"
    )
    return 0


def _approve(args: argparse.Namespace) -> int:
    network = resolve_network(args.network)
    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not bool(args.yes),
    )
    handle, wallet = load_wallet(
        coldkey_name=coldkey_name,
        hotkey_name=hotkey_name,
    )
    key_kind: KeyKind = args.key_kind
    signer = signer_address(live_wallet=wallet, key_kind=key_kind)
    with ApiClient(base_url=network.api_url) as client:
        if args.user_code:
            public = client.get_miner_device(args.user_code)
            user_code = public.user_code
            ttl_seconds = public.ttl_seconds
            scopes = ",".join(public.scopes)
            grant_id = public.grant_id
            oauth_client_id = public.oauth_client_id
            redirect_uri = public.redirect_uri
        else:
            hours = max(1, min(int(args.hours), 720))
            started = client.start_miner_device(
                MinerDeviceStartRequest(
                    scopes=[
                        part.strip()  # type: ignore[misc]
                        for part in str(args.scopes).split(",")
                        if part.strip()
                    ],
                    ttl_seconds=hours * 3600,
                )
            )
            public = client.get_miner_device(started.user_code)
            user_code = public.user_code
            ttl_seconds = public.ttl_seconds
            scopes = ",".join(public.scopes)
            grant_id = public.grant_id
            oauth_client_id = public.oauth_client_id
            redirect_uri = public.redirect_uri
            print(started.verification_uri_complete, file=sys.stderr)
        nonce = uuid4()
        issued_at = datetime.now(UTC)
        try:
            payload = login_message(
                netuid=args.netuid,
                miner_hotkey=handle.hotkey_ss58,
                user_code=user_code,
                grant_id=grant_id,
                ttl_seconds=ttl_seconds,
                scopes=scopes,
                nonce=nonce,
                issued_at=issued_at,
                key_kind=key_kind,
                signer=signer,
                oauth_client_id=oauth_client_id,
                redirect_uri=redirect_uri,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        signature = sign_payload(live_wallet=wallet, key_kind=key_kind, payload=payload)
        body = MinerLoginApproveRequest(
            netuid=args.netuid,
            miner_hotkey=handle.hotkey_ss58,
            nonce=nonce,
            issued_at=issued_at,
            proof=NameClaimProof(
                key_kind=key_kind,
                signer=signer,
                signature=signature,
            ),
        )
        if args.print_only:
            print(body.model_dump_json(indent=2))
            return 0
        confirm_login_action(
            user_code=user_code,
            hotkey_ss58=handle.hotkey_ss58,
            scopes=scopes,
            hours=max(1, ttl_seconds // 3600),
            skip=bool(args.yes),
            oauth_client_name=public.oauth_client_name,
            redirect_uri=redirect_uri,
        )
        result = client.approve_miner_device(user_code, body)
    if result.access_token and result.session:
        saved = save_miner_session(
            network=network.name,
            token=result.access_token,
            hotkey=result.session.miner_hotkey,
            scopes=list(result.session.scopes),
            expires_at=result.session.expires_at.isoformat(),
        )
        if not saved:
            print(
                "signed in, but the local session file was not saved", file=sys.stderr
            )
    signed = result.session.miner_hotkey if result.session else handle.hotkey_ss58
    print(f"signed in as {signed}")
    if result.continue_url:
        print(result.continue_url)
    return 0
