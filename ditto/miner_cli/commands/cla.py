"""Download pinned CLA text and sign only after explicit digest-bound consent."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ditto.api_models.contributor_identity import ClaSignRequest, cla_message
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "cla",
        help="Read or sign the pinned contributor licence agreement.",
        parents=parents or [],
    )
    subs = parser.add_subparsers(dest="cla_command", required=True)
    for command in ("sign", "status"):
        sub = subs.add_parser(command)
        sub.set_defaults(func=run)
        sub.add_argument(
            "--wallet.name",
            "--coldkey",
            dest="coldkey_name",
            default=os.environ.get("WALLET_NAME"),
        )
        sub.add_argument(
            "--wallet.hotkey",
            "--hotkey",
            dest="hotkey_name",
            default=os.environ.get("HOTKEY_NAME"),
        )
        sub.add_argument("--json", action="store_true")
        if command == "sign":
            sub.add_argument(
                "--version",
                type=int,
                help=(
                    "Also permit signing a published future version before its "
                    "effective date."
                ),
            )
            sub.add_argument(
                "--output",
                type=Path,
                help="Where to save the exact agreement for review.",
            )
            sub.add_argument(
                "--confirmation",
                help=(
                    "Explicit typed consent: SIGN CLA <version> <sha256>. No "
                    "--yes shortcut."
                ),
            )
    return parser


def run(args: argparse.Namespace) -> int:
    network = resolve_network(args.network)
    try:
        names = resolve_wallet_names(
            coldkey_name=args.coldkey_name,
            hotkey_name=args.hotkey_name,
            interactive=sys.stdin.isatty(),
        )
        handle, wallet = load_wallet(coldkey_name=names[0], hotkey_name=names[1])
        with ApiClient(base_url=network.api_url) as client:
            status = client.get_cla_status(hotkey=handle.hotkey_ss58)
            if status is None:
                raise MinerCliError("this Platform does not support CLA signing yet")
            if args.cla_command == "status":
                if args.json:
                    print(
                        json.dumps(
                            status.model_dump(mode="json"), indent=2, sort_keys=True
                        )
                    )
                else:
                    signed = (
                        f"v{status.signature.cla_version} at "
                        f"{status.signature.signed_at.isoformat()}"
                        if status.signature
                        else "none"
                    )
                    current = (
                        f"v{status.current.version}"
                        if status.current
                        else "none published"
                    )
                    print(
                        f"Signed: {signed}; effective: {current}; current: "
                        f"{status.is_current}; gate: {status.mode}"
                    )
                return 0
            version = (
                client.get_cla_version(version=args.version)
                if args.version is not None
                else status.current
            )
            if version is None:
                raise MinerCliError(
                    "no effective CLA has been published; maintainers must "
                    "provide approved text"
                )
            raw = client.get_cla_text(version=version.version)
            digest = hashlib.sha256(raw).hexdigest()
            if digest != version.sha256:
                raise MinerCliError("CLA digest mismatch; refusing to sign")
            raw.decode("utf-8")  # A binary or invalid UTF-8 document is not reviewable.
            target = (
                args.output
                or Path.home()
                / ".cache"
                / "ditto"
                / "cla"
                / network.name
                / f"CLA-v{version.version}-{digest}.md"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            confirmation = f"SIGN CLA {version.version} {digest}"
            print(
                f"Review the complete agreement at {target.resolve()}\nVersion: "
                f"{version.version}\nSHA-256: {digest}\n"
                f"Effective: {version.effective_at.isoformat()}",
                file=sys.stderr,
            )
            value = (
                args.confirmation
                if args.confirmation is not None
                else input(
                    f"Type exactly '{confirmation}' to sign with hotkey "
                    f"{handle.hotkey_ss58}: "
                )
            )
            if value != confirmation:
                raise MinerCliError(
                    "CLA was not signed: explicit version and digest "
                    "confirmation did not match"
                )
            nonce, issued = uuid4(), datetime.now(UTC)
            payload = cla_message(
                netuid=status.netuid,
                cla_version=version.version,
                cla_sha256=digest,
                hotkey=handle.hotkey_ss58,
                nonce=nonce,
                issued_at=issued,
            )
            result = client.sign_cla(
                ClaSignRequest(
                    netuid=status.netuid,
                    hotkey=handle.hotkey_ss58,
                    cla_version=version.version,
                    cla_sha256=digest,
                    nonce=nonce,
                    issued_at=issued,
                    signature=wallet.hotkey.sign(payload).hex(),
                )
            )
            if args.json:
                print(
                    json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True)
                )
            else:
                print(
                    f"Signed CLA v{result.cla_version} at "
                    f"{result.signed_at.isoformat()}"
                )
            return 0
    except (MinerCliError, ValueError, EOFError, KeyboardInterrupt, OSError) as exc:
        print(str(exc) or "CLA signing cancelled", file=sys.stderr)
        return 1
