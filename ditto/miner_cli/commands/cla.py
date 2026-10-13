"""``ditto cla``: sign or inspect the SN118 Contributor License Agreement.

Subcommands:
- ``ditto cla sign``    verify SHA-256 of docs/legal/CLA-v<N>.md, prompt for explicit typed confirmation, and sign with the hotkey (sr25519)
- ``ditto cla status``  display signed version, whether it is current, and signed_at timestamp
"""

from __future__ import annotations

import argparse
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ditto.miner_cli.cla import cla_message, sign_cla_payload, verify_cla_digest
from ditto.miner_cli.errors import MinerCliError

CONFIRMATION_PHRASE = "I AGREE TO THE SN118 CLA"


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "cla",
        help="Sign or check status of the SN118 Contributor License Agreement.",
        parents=parents or [],
    )
    sub = parser.add_subparsers(dest="cla_subcommand", required=True)

    sign_p = sub.add_parser("sign", help="Sign the current effective CLA with your hotkey.")
    sign_p.add_argument("--netuid", type=int, default=118)
    sign_p.add_argument("--cla-file", type=Path, default=None, help="Optional local path to CLA-v<N>.md")
    sign_p.add_argument("--confirm", type=str, default=None, help=f"Type '{CONFIRMATION_PHRASE}' to confirm")

    status_p = sub.add_parser("status", help="Show CLA signature status for your hotkey.")
    status_p.add_argument("--hotkey", type=str, default=None)
    return parser


def run_cla_sign(
    *,
    api_client,
    wallet,
    netuid: int = 118,
    cla_file: Path | None = None,
    typed_confirmation: str | None = None,
) -> dict:
    current = api_client.get_current_cla_version()
    version = int(current["version"])
    expected_sha256 = str(current["sha256"])
    cla_text = (
        cla_file.read_text(encoding="utf-8")
        if cla_file is not None
        else str(current["text"])
    )

    verified_sha = verify_cla_digest(content=cla_text, expected_sha256=expected_sha256)
    print(f"CLA v{version} (sha256: {verified_sha})\n", file=sys.stderr)
    print(cla_text, file=sys.stderr)

    if typed_confirmation is None:
        typed_confirmation = input(f"Type '{CONFIRMATION_PHRASE}' to sign with hotkey: ").strip()
    if typed_confirmation != CONFIRMATION_PHRASE:
        raise MinerCliError("Explicit typed confirmation did not match; CLA was not signed.")

    hotkey = str(wallet.hotkey.ss58_address)
    nonce = uuid.uuid4()
    issued_at = datetime.now(UTC)
    payload = cla_message(
        netuid=netuid,
        cla_version=version,
        cla_sha256=verified_sha,
        hotkey=hotkey,
        nonce=nonce,
        issued_at=issued_at,
    )
    signature = sign_cla_payload(live_wallet=wallet, payload=payload)
    return api_client.submit_cla_signature(
        {
            "netuid": netuid,
            "hotkey": hotkey,
            "cla_version": version,
            "cla_sha256": verified_sha,
            "nonce": str(nonce),
            "issued_at": issued_at.isoformat(),
            "signature": signature,
        }
    )
