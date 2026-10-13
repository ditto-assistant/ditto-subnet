"""Link a GitHub account with browser consent and a signed miner attestation."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import webbrowser
from datetime import UTC, datetime
from uuid import uuid4

from ditto.api_models.contributor_identity import (
    ContributorProof,
    GithubLinkRequest,
    GithubUnlinkRequest,
    github_link_message,
    github_unlink_message,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.preferences import load_miner_session
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "link-github",
        help="Verify a GitHub account and sign its link to your miner hotkey.",
        parents=parents or [],
    )
    parser.set_defaults(func=run, github_command="link")
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
    )
    parser.add_argument("--key-kind", choices=("hotkey", "coldkey"), default="hotkey")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--wait",
        type=int,
        default=600,
        help="Seconds to wait for browser consent (maximum 600).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Sign the printed pairing after the GitHub browser holder approves it.",
    )
    parser.add_argument("--json", action="store_true")
    subs = parser.add_subparsers(dest="github_command")
    status = subs.add_parser("status", help="Read the active verified link.")
    status.set_defaults(func=run, github_command="status")
    status.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    unlink = subs.add_parser(
        "unlink", help="Sign a revocation of the exact active link."
    )
    unlink.set_defaults(func=run, github_command="unlink")
    return parser


def _print(link, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                link.model_dump(mode="json") if link else None, indent=2, sort_keys=True
            )
        )
    elif link:
        print(
            f"{link.hotkey} is linked to GitHub @{link.github_login} (id "
            f"{link.github_user_id}) since {link.linked_at.isoformat()}"
        )
    else:
        print("no verified GitHub account is linked")


def run(args: argparse.Namespace) -> int:
    network = resolve_network(args.network)
    saved = load_miner_session(network=network.name)
    if saved is None:
        print(
            "run `ditto login` first with the read and profile scopes", file=sys.stderr
        )
        return 1
    token, hotkey = str(saved["token"]), str(saved["hotkey"])
    try:
        with ApiClient(base_url=network.api_url) as client:
            if args.github_command == "status":
                _print(client.get_github_link(token=token).link, bool(args.json))
                return 0
            names = resolve_wallet_names(
                coldkey_name=args.coldkey_name,
                hotkey_name=args.hotkey_name,
                interactive=sys.stdin.isatty() and not args.yes,
            )
            if args.github_command == "unlink":
                linked = client.get_github_link(token=token).link
                if linked is None:
                    print("no active GitHub link to revoke")
                    return 0
                print(
                    f"Revoke @{linked.github_login} (id "
                    f"{linked.github_user_id}) from hotkey {hotkey}",
                    file=sys.stderr,
                )
                if (
                    not args.yes
                    and input("Type UNLINK to sign this revocation: ").strip()
                    != "UNLINK"
                ):
                    return 1
                handle, wallet = load_wallet(
                    coldkey_name=names[0], hotkey_name=names[1]
                )
                if handle.hotkey_ss58 != hotkey:
                    raise MinerCliError(
                        "selected wallet does not match the signed-in miner hotkey"
                    )
                signer = (
                    wallet.hotkey.ss58_address
                    if args.key_kind == "hotkey"
                    else wallet.coldkeypub.ss58_address
                )
                nonce, issued = uuid4(), datetime.now(UTC)
                payload = github_unlink_message(
                    netuid=linked.netuid,
                    hotkey=hotkey,
                    link_id=linked.link_id,
                    nonce=nonce,
                    issued_at=issued,
                    key_kind=args.key_kind,
                    signer=signer,
                )
                key = wallet.hotkey if args.key_kind == "hotkey" else wallet.coldkey
                body = GithubUnlinkRequest(
                    netuid=linked.netuid,
                    hotkey=hotkey,
                    link_id=linked.link_id,
                    nonce=nonce,
                    issued_at=issued,
                    proof=ContributorProof(
                        key_kind=args.key_kind,
                        signer=signer,
                        signature=key.sign(payload).hex(),
                    ),
                )
                client.unlink_github(token=token, body=body)
                print("GitHub link revoked; its signed history is retained")
                return 0
            started = client.start_github_link(token=token)
            print(
                f"Approve the GitHub account and this hotkey in your browser: "
                f"{started.authorize_url}",
                file=sys.stderr,
            )
            if not args.no_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(started.authorize_url, new=2)
            deadline = time.monotonic() + max(1, min(args.wait, started.expires_in))
            while time.monotonic() < deadline:
                attempt = client.get_github_link_attempt(
                    token=token, attempt_id=started.attempt_id
                )
                if attempt.status in ("failed", "expired"):
                    raise MinerCliError(
                        f"GitHub link {attempt.status}: "
                        f"{attempt.error or 'start again'}"
                    )
                if attempt.status == "accepted":
                    if attempt.github_user_id is None or attempt.github_login is None:
                        raise MinerCliError(
                            "approved attempt has no verified GitHub identity"
                        )
                    print(
                        f"GitHub verified @{attempt.github_login} (id "
                        f"{attempt.github_user_id}); miner hotkey {hotkey}",
                        file=sys.stderr,
                    )
                    if (
                        not args.yes
                        and input("Type LINK to sign this exact pairing: ").strip()
                        != "LINK"
                    ):
                        print("not linked", file=sys.stderr)
                        return 1
                    # Load private wallet material only after identity is shown.
                    handle, wallet = load_wallet(
                        coldkey_name=names[0], hotkey_name=names[1]
                    )
                    if handle.hotkey_ss58 != hotkey:
                        raise MinerCliError(
                            "selected wallet does not match the signed-in miner hotkey"
                        )
                    signer = (
                        wallet.hotkey.ss58_address
                        if args.key_kind == "hotkey"
                        else wallet.coldkeypub.ss58_address
                    )
                    nonce, issued = uuid4(), datetime.now(UTC)
                    payload = github_link_message(
                        netuid=started.netuid,
                        hotkey=hotkey,
                        github_user_id=attempt.github_user_id,
                        github_login=attempt.github_login,
                        link_attempt_id=attempt.attempt_id,
                        nonce=nonce,
                        issued_at=issued,
                        key_kind=args.key_kind,
                        signer=signer,
                    )
                    key = wallet.hotkey if args.key_kind == "hotkey" else wallet.coldkey
                    body = GithubLinkRequest(
                        netuid=started.netuid,
                        hotkey=hotkey,
                        github_user_id=attempt.github_user_id,
                        github_login=attempt.github_login,
                        link_attempt_id=attempt.attempt_id,
                        nonce=nonce,
                        issued_at=issued,
                        proof=ContributorProof(
                            key_kind=args.key_kind,
                            signer=signer,
                            signature=key.sign(payload).hex(),
                        ),
                    )
                    _print(
                        client.confirm_github_link(token=token, body=body),
                        bool(args.json),
                    )
                    return 0
                time.sleep(2)
            raise MinerCliError(
                "timed out waiting for GitHub browser consent; start again"
            )
    except (MinerCliError, EOFError, KeyboardInterrupt) as exc:
        print(str(exc) or "GitHub linking cancelled", file=sys.stderr)
        return 1
