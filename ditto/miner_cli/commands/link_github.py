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
from typing import Any
from uuid import uuid4

from ditto.api_models.contributor_identity import (
    ContributorProof,
    GithubLinkRequest,
    GithubUnlinkRequest,
    github_link_message,
    github_unlink_message,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.confirm import prompt_input
from ditto.miner_cli.errors import MinerCliError, TransientApiError
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
        help=(
            "Sign the pairing without a prompt once the browser approves it; "
            "requires --expect-github-id."
        ),
    )
    parser.add_argument(
        "--expect-github-id",
        type=_github_user_id,
        default=None,
        metavar="ID",
        help="Numeric GitHub account ID that must approve the link.",
    )
    parser.add_argument("--json", action="store_true")
    subs = parser.add_subparsers(dest="github_command")
    status = subs.add_parser(
        "status", help="Read the active verified link.", parents=parents or []
    )
    status.set_defaults(func=run, github_command="status")
    status.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    unlink = subs.add_parser(
        "unlink",
        help="Sign a revocation of the exact active link.",
        parents=parents or [],
    )
    # Suppressed child defaults preserve values supplied before the subcommand.
    unlink.add_argument(
        "--wallet.name", "--coldkey", dest="coldkey_name", default=argparse.SUPPRESS
    )
    unlink.add_argument(
        "--wallet.hotkey", "--hotkey", dest="hotkey_name", default=argparse.SUPPRESS
    )
    unlink.add_argument(
        "--key-kind", choices=("hotkey", "coldkey"), default=argparse.SUPPRESS
    )
    unlink.add_argument("--yes", action="store_true", default=argparse.SUPPRESS)
    unlink.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    unlink.set_defaults(func=run, github_command="unlink")
    return parser


def _github_user_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= 9223372036854775807:
        raise argparse.ArgumentTypeError("expected a positive numeric GitHub user id")
    return number


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


def _wallet_names(args: argparse.Namespace) -> tuple[str, str]:
    return resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not args.yes,
    )


def _signing_key(wallet: Any, key_kind: str) -> tuple[str, Any]:
    try:
        key = wallet.hotkey if key_kind == "hotkey" else wallet.coldkey
        return key.ss58_address, key
    except Exception:
        raise MinerCliError(
            f"cannot load or unlock the {key_kind} signing key; "
            "check the wallet keyfile and passphrase"
        ) from None


def _sign_payload(key: Any, payload: bytes) -> str:
    try:
        return key.sign(payload).hex()
    except Exception:
        raise MinerCliError(
            "cannot sign GitHub identity proof; check the wallet signing key and retry"
        ) from None


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
            if args.github_command == "unlink":
                status = client.get_github_link(token=token)
                linked = status.link
                if linked is None:
                    print(
                        json.dumps({"link_id": None, "revoked": False})
                        if args.json
                        else "no active GitHub link to revoke"
                    )
                    return 0
                names = _wallet_names(args)
                print(
                    f"Revoke @{linked.github_login} (id "
                    f"{linked.github_user_id}) from hotkey {hotkey}",
                    file=sys.stderr,
                )
                if (
                    not args.yes
                    and prompt_input("Type UNLINK to sign this revocation: ").strip()
                    != "UNLINK"
                ):
                    print("not unlinked", file=sys.stderr)
                    return 1
                handle, wallet = load_wallet(
                    coldkey_name=names[0], hotkey_name=names[1]
                )
                if handle.hotkey_ss58 != hotkey:
                    raise MinerCliError(
                        "selected wallet does not match the signed-in miner hotkey"
                    )
                signer, key = _signing_key(wallet, args.key_kind)
                nonce, issued = uuid4(), datetime.now(UTC)
                payload = github_unlink_message(
                    netuid=status.netuid,
                    hotkey=hotkey,
                    link_id=linked.link_id,
                    nonce=nonce,
                    issued_at=issued,
                    key_kind=args.key_kind,
                    signer=signer,
                )
                body = GithubUnlinkRequest(
                    netuid=status.netuid,
                    hotkey=hotkey,
                    link_id=linked.link_id,
                    nonce=nonce,
                    issued_at=issued,
                    proof=ContributorProof(
                        key_kind=args.key_kind,
                        signer=signer,
                        signature=_sign_payload(key, payload),
                    ),
                )
                client.unlink_github(token=token, body=body)
                print(
                    json.dumps({"link_id": str(linked.link_id), "revoked": True})
                    if args.json
                    else "GitHub link revoked; its signed history is retained"
                )
                return 0
            if args.yes and args.expect_github_id is None:
                # Whoever opens the launch URL first approves the account; an
                # unattended run must not sign an account nobody checked.
                raise MinerCliError(
                    "--yes requires --expect-github-id <numeric GitHub user id>"
                )
            names = _wallet_names(args)
            started = client.start_github_link(token=token)
            print(
                f"Approve the GitHub account and this hotkey in your browser: "
                f"{started.authorize_url}",
                file=sys.stderr,
            )
            if not args.no_browser:
                with contextlib.suppress(Exception):
                    webbrowser.open(started.authorize_url, new=2)
            deadline = time.monotonic() + max(
                1, min(args.wait, 600, started.expires_in)
            )
            last_transient: TransientApiError | None = None
            while time.monotonic() < deadline:
                try:
                    attempt = client.get_github_link_attempt(
                        token=token, attempt_id=started.attempt_id
                    )
                except TransientApiError as exc:
                    last_transient = exc
                    remaining = max(0, deadline - time.monotonic())
                    if remaining:
                        time.sleep(min(2, remaining))
                    continue
                last_transient = None
                if attempt.status == "linked":
                    link = client.get_github_link(token=token).link
                    if link is None or link.link_attempt_id != started.attempt_id:
                        raise MinerCliError("completed link changed; read its status")
                    _print(link, bool(args.json))
                    return 0
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
                        args.expect_github_id is not None
                        and attempt.github_user_id != args.expect_github_id
                    ):
                        raise MinerCliError(
                            "the approving GitHub account is not the expected id "
                            f"{args.expect_github_id}; not linked"
                        )
                    current = client.get_github_link(token=token).link
                    if (
                        current is not None
                        and current.github_user_id != attempt.github_user_id
                    ):
                        print(
                            "Linking revokes the active link to "
                            f"@{current.github_login} (id {current.github_user_id})",
                            file=sys.stderr,
                        )
                    if (
                        not args.yes
                        and prompt_input(
                            "Type LINK to sign this exact pairing: "
                        ).strip()
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
                    signer, key = _signing_key(wallet, args.key_kind)
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
                            signature=_sign_payload(key, payload),
                        ),
                    )
                    _print(
                        client.confirm_github_link(token=token, body=body),
                        bool(args.json),
                    )
                    return 0
                time.sleep(2)
            if last_transient is not None:
                # Polling never recovered; report the cause, not just a timeout.
                raise MinerCliError(
                    "timed out waiting for GitHub browser consent; Platform was "
                    f"unavailable ({last_transient}); start again"
                )
            raise MinerCliError(
                "timed out waiting for GitHub browser consent; start again"
            )
    except (MinerCliError, ValueError, EOFError, OSError) as exc:
        print(str(exc) or "GitHub linking cancelled", file=sys.stderr)
        return 1
