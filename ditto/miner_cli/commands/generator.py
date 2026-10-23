"""``ditto generator``: the DittoBench generator competition (shadow).

The counterbalance to the agent competition. A generator miner runs their own
program to emit a **challenge pack** (a memory haystack plus cases with
deterministic gold answers), validates it locally with the same rules the
scorer applies, and uploads it signed by their hotkey. The scorer runs the
pack against the top agents; packs earn credit for cases strong agents split
on. The lane is weight-zero while in shadow.

* ``validate PACK``: run the contract validator (``challenge validate``).
* ``upload PACK``: validate, sign ``ditto-generator-pack-v1``, and upload.
* ``status``: list your packs and their outcomes.
* ``board``: the shadow generator ledger.

See ``research/dittobench-datagen/docs/generator-competition.md`` and
``miners/dittobench-generator-starter-kit``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

PACK_CONTRACT_V1 = "dittobench-challenge-pack/v1"
MAX_PACK_BYTES = 4 << 20
SIGNATURE_DOMAIN = "ditto-generator-pack-v1"
_DATAGEN_DIR = Path(__file__).resolve().parents[3] / "research" / "dittobench-datagen"


class PackValidationError(MinerCliError):
    """The pack failed local validation."""


def build_pack_payload(
    *, hotkey_ss58: str, pack_sha256: str, signature_timestamp: int, nonce: str
) -> bytes:
    """The exact bytes signed for an upload; Platform verifies the same."""
    return (
        f"{SIGNATURE_DOMAIN}:{hotkey_ss58}:{pack_sha256}:{signature_timestamp}:{nonce}"
    ).encode("ascii")


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "generator",
        help="Generator competition: validate and upload challenge packs.",
        description=(
            "Submit DittoBench challenge packs: cases the top agents split on. "
            "Weight-zero while the competition is in shadow."
        ),
        parents=parents or [],
    )
    subs = parser.add_subparsers(dest="generator_command", required=True)

    validate = subs.add_parser("validate", help="Validate a pack locally.")
    validate.add_argument("pack", type=Path)
    _validator_flag(validate)
    validate.set_defaults(func=run)

    upload = subs.add_parser("upload", help="Validate, sign, and upload a pack.")
    upload.add_argument("pack", type=Path)
    _validator_flag(upload)
    upload.add_argument(
        "--skip-validate",
        action="store_true",
        help="Upload without local validation (the scorer still validates).",
    )
    _wallet_flags(upload)
    upload.set_defaults(func=run)

    status = subs.add_parser("status", help="List a hotkey's packs.")
    status.add_argument("--ss58", dest="ss58", help="Hotkey address to list.")
    _wallet_flags(status)
    status.set_defaults(func=run)

    board = subs.add_parser("board", help="Show the shadow generator ledger.")
    board.set_defaults(func=run)
    return parser


def _validator_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--validator",
        dest="validator",
        default=os.environ.get("DITTO_CHALLENGE_BIN"),
        help=(
            "Path to the `challenge` binary. Default: `challenge` on PATH, else "
            "`go run ./cmd/challenge` from this checkout."
        ),
    )


def _wallet_flags(parser: argparse.ArgumentParser) -> None:
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
    parser.add_argument("-y", "--yes", action="store_true")


def _validator_command(explicit: str | None) -> tuple[list[str], Path | None] | None:
    if explicit:
        return [explicit], None
    found = shutil.which("challenge")
    if found:
        return [found], None
    if shutil.which("go") and (_DATAGEN_DIR / "cmd" / "challenge").is_dir():
        return ["go", "run", "./cmd/challenge"], _DATAGEN_DIR
    return None


def validate_pack(path: Path, *, validator: str | None) -> dict:
    """Run the contract validator and return its JSON report."""
    command = _validator_command(validator)
    if command is None:
        raise PackValidationError(
            "no pack validator found: install Go and run from a ditto-subnet "
            "checkout, or pass --validator /path/to/challenge"
        )
    argv, cwd = command
    proc = subprocess.run(
        [*argv, "validate", "-json", str(path.resolve())],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PackValidationError(
            f"validator failed to run: {proc.stderr.strip()[:400]}"
        ) from exc
    if not isinstance(report, dict):
        raise PackValidationError("validator returned a non-object report")
    return report


def _print_report(report: dict) -> None:
    for issue in report.get("issues") or []:
        where = f"case {issue['case_id']}" if issue.get("case_id") else "pack"
        print(f"{where}: [{issue.get('rule')}] {issue.get('message')}", file=sys.stderr)
    if report.get("valid"):
        print(
            f"valid: {report.get('case_count')} cases, {report.get('pair_count')} "
            f"pairs, sha256 {report.get('pack_sha256')}",
            file=sys.stderr,
        )


def _read_pack(path: Path) -> bytes:
    raw = path.read_bytes()
    if not raw or len(raw) > MAX_PACK_BYTES:
        raise PackValidationError(f"pack must be 1..{MAX_PACK_BYTES} bytes")
    return raw


def run(args: argparse.Namespace) -> int:
    network = resolve_network(args.network)
    command = args.generator_command

    if command == "board":
        with ApiClient(base_url=network.api_url) as client:
            print(json.dumps(client.get_generator_board(), indent=2))
        return 0

    if command == "validate":
        report = validate_pack(args.pack, validator=args.validator)
        _print_report(report)
        return 0 if report.get("valid") else 1

    if command == "status":
        hotkey = args.ss58
        if not hotkey:
            coldkey_name, hotkey_name = resolve_wallet_names(
                coldkey_name=args.coldkey_name,
                hotkey_name=args.hotkey_name,
                interactive=sys.stdin.isatty() and not args.yes,
            )
            handle, _ = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
            hotkey = handle.hotkey_ss58
        with ApiClient(base_url=network.api_url) as client:
            print(json.dumps(client.list_generator_packs(hotkey=hotkey), indent=2))
        return 0

    if command == "upload":
        raw = _read_pack(args.pack)
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("contract") != PACK_CONTRACT_V1:
            raise PackValidationError(f"pack contract must be {PACK_CONTRACT_V1!r}")
        if not args.skip_validate:
            report = validate_pack(args.pack, validator=args.validator)
            _print_report(report)
            if not report.get("valid"):
                raise PackValidationError("pack is invalid; fix the issues above")
        coldkey_name, hotkey_name = resolve_wallet_names(
            coldkey_name=args.coldkey_name,
            hotkey_name=args.hotkey_name,
            interactive=sys.stdin.isatty() and not args.yes,
        )
        handle, wallet = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
        if doc.get("miner_hotkey") != handle.hotkey_ss58:
            raise PackValidationError(
                f"pack miner_hotkey {doc.get('miner_hotkey')!r} is not the signing "
                f"hotkey {handle.hotkey_ss58}; regenerate the pack for this hotkey"
            )
        digest = hashlib.sha256(raw).hexdigest()
        nonce = str(uuid4())
        timestamp = int(time.time())
        signature = wallet.hotkey.sign(
            build_pack_payload(
                hotkey_ss58=handle.hotkey_ss58,
                pack_sha256=digest,
                signature_timestamp=timestamp,
                nonce=nonce,
            )
        ).hex()
        body = {
            "hotkey": handle.hotkey_ss58,
            "pack_sha256": digest,
            "pack_base64": base64.b64encode(raw).decode("ascii"),
            "signature": signature,
            "signature_timestamp": timestamp,
            "signature_nonce": nonce,
        }
        with ApiClient(base_url=network.api_url) as client:
            result = client.post_generator_pack(body)
        print(result["pack_id"])
        print(
            f"pack {result['status']}: {result['case_count']} cases, "
            f"sha256 {result['pack_sha256']}",
            file=sys.stderr,
        )
        return 0

    raise MinerCliError(f"unknown generator command {command!r}")
