"""``ditto generator``: the DittoBench generator competition (shadow).

The counterbalance to the agent competition. A generator miner writes a
CPU-only Go program that turns a seed into a **challenge pack** (a memory
haystack plus cases with deterministic gold answers), rehearses it locally
exactly as the scorer runs it, and uploads its source signed by their hotkey.
The scorer builds the source offline, runs it in a no-network sandbox on fresh
secret seeds, and runs the packs against the top agents; generators earn
credit for cases strong agents split on. The lane is weight-zero in shadow.

* ``rehearse DIR``: build and run the generator as the scorer does
  (``challenge rehearse``; ``--sandbox docker`` uses the scorer's images).
* ``package DIR``: write the deterministic, vendored source archive.
* ``upload [ARCHIVE | --dir DIR]``: check, pay the upload fee when Platform
  requires one, sign ``ditto-generator-v1``, upload.
* ``status``: list your generators, their qualification, and latest round score.
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
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import bittensor

from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands.upload import _recovery_receipt_from_args
from ditto.miner_cli.confirm import confirm_payment
from ditto.miner_cli.errors import MinerCliError
from ditto.miner_cli.models import PaymentReceipt, WalletHandle
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.payment import preflight_payment_signer, submit_eval_payment
from ditto.miner_cli.preferences import (
    GENERATOR_PAYMENTS_BUCKET,
    clear_pending_payment,
    load_pending_payment,
    save_pending_payment,
)
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

MAX_ARCHIVE_BYTES = 4 << 20
SIGNATURE_DOMAIN = "ditto-generator-v1"
# Local key for a saved generator fee proof (kept apart from agent proofs).
PENDING_PAYMENT_NAME = "generator"
_DATAGEN_DIR = Path(__file__).resolve().parents[3] / "research" / "dittobench-datagen"


class GeneratorToolError(MinerCliError):
    """The local generator tooling failed or rejected the generator."""


def build_source_payload(
    *, hotkey_ss58: str, source_sha256: str, signature_timestamp: int, nonce: str
) -> bytes:
    """The exact bytes signed for an upload; Platform verifies the same."""
    return (
        f"{SIGNATURE_DOMAIN}:{hotkey_ss58}:{source_sha256}:{signature_timestamp}:{nonce}"
    ).encode("ascii")


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "generator",
        help="Generator competition: rehearse, package, and upload generators.",
        description=(
            "Submit DittoBench case generators: CPU-only programs scored on "
            "secret seeds by how sharply their cases split the top agents. "
            "Weight-zero while the competition is in shadow."
        ),
        parents=parents or [],
    )
    subs = parser.add_subparsers(dest="generator_command", required=True)

    rehearse = subs.add_parser(
        "rehearse", help="Build and run a generator exactly as the scorer does."
    )
    rehearse.add_argument("dir", type=Path)
    rehearse.add_argument("--ss58", dest="ss58", help="Hotkey the packs bind to.")
    rehearse.add_argument("--seeds", type=int, default=3)
    rehearse.add_argument(
        "--cases",
        type=int,
        default=80,
        help=(
            "Cases per pack. Platform's cases_per_seed can be raised to 80 "
            "between rounds, and a generator that cannot fill it is rejected, "
            "so rehearse at the cap (the default)."
        ),
    )
    rehearse.add_argument(
        "--sandbox",
        choices=("local", "docker"),
        default="local",
        help="docker runs the scorer's pinned no-network sandbox.",
    )
    rehearse.add_argument("--out-dir", type=Path, help="Write each seed's pack here.")
    _tool_flag(rehearse)
    _wallet_flags(rehearse)
    rehearse.set_defaults(func=run)

    package = subs.add_parser("package", help="Write the upload archive.")
    package.add_argument("dir", type=Path)
    package.add_argument(
        "-o", "--out", type=Path, default=Path("generator.tar.gz"), dest="out"
    )
    _tool_flag(package)
    package.set_defaults(func=run)

    upload = subs.add_parser(
        "upload",
        help="Check, pay the fee if required, sign, and upload generator source.",
    )
    upload.add_argument("archive", type=Path, nargs="?")
    upload.add_argument("--dir", type=Path, help="Package this directory first.")
    _tool_flag(upload)
    _wallet_flags(upload)
    recovery = upload.add_argument_group(
        "payment recovery",
        "Reuse a finalized fee payment after an upload failed or its response "
        "was lost. All three are required together; no new transfer is sent.",
    )
    recovery.add_argument("--payment-block-hash")
    recovery.add_argument("--payment-block-number", type=int)
    recovery.add_argument("--payment-extrinsic-index", type=int)
    upload.set_defaults(func=run)

    status = subs.add_parser("status", help="List a hotkey's generators.")
    status.add_argument("--ss58", dest="ss58", help="Hotkey address to list.")
    _wallet_flags(status)
    status.set_defaults(func=run)

    board = subs.add_parser("board", help="Show the shadow generator ledger.")
    board.set_defaults(func=run)
    return parser


def _tool_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--challenge-bin",
        dest="challenge_bin",
        default=os.environ.get("DITTO_CHALLENGE_BIN"),
        help=(
            "Path to the `challenge` tool. Default: `challenge` on PATH, else "
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


def _tool_command(explicit: str | None) -> tuple[list[str], Path | None]:
    if explicit:
        return [explicit], None
    found = shutil.which("challenge")
    if found:
        return [found], None
    if shutil.which("go") and (_DATAGEN_DIR / "cmd" / "challenge").is_dir():
        return ["go", "run", "./cmd/challenge"], _DATAGEN_DIR
    raise GeneratorToolError(
        "no generator tool found: install Go and run from a ditto-subnet "
        "checkout, or pass --challenge-bin /path/to/challenge"
    )


def run_tool(
    args: list[str], *, explicit: str | None, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run the datagen ``challenge`` tool; paths in ``args`` must be absolute."""
    argv, cwd = _tool_command(explicit)
    return subprocess.run(
        [*argv, *args], cwd=cwd, capture_output=capture, text=True, check=False
    )


def inspect_archive(path: Path, *, explicit: str | None) -> dict:
    """Run the contract's archive check and return its identity JSON."""
    proc = run_tool(["inspect", str(path.resolve())], explicit=explicit, capture=True)
    if proc.returncode != 0:
        raise GeneratorToolError(
            f"archive rejected: {(proc.stderr or proc.stdout).strip()[:600]}"
        )
    info = json.loads(proc.stdout)
    if not isinstance(info, dict):
        raise GeneratorToolError("archive check returned a non-object")
    return info


def package_dir(directory: Path, out: Path, *, explicit: str | None) -> None:
    proc = run_tool(
        ["pack", "-dir", str(directory.resolve()), "-out", str(out.resolve())],
        explicit=explicit,
    )
    if proc.returncode != 0:
        raise GeneratorToolError(f"packaging {directory} failed")


def _resolve_hotkey(args: argparse.Namespace) -> str:
    if getattr(args, "ss58", None):
        return str(args.ss58)
    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not args.yes,
    )
    handle, _ = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
    return str(handle.hotkey_ss58)


def _pay_fee(
    *,
    args: argparse.Namespace,
    terms: dict[str, Any],
    handle: WalletHandle,
    wallet: bittensor.Wallet,
    coldkey_name: str,
    subtensor_network: str,
) -> PaymentReceipt:
    """Pay the quoted upload fee through the agent-upload payment path."""
    amount_rao = terms.get("payment_amount_rao")
    send_address = terms.get("payment_send_address")
    if not isinstance(amount_rao, int) or not isinstance(send_address, str):
        raise MinerCliError("platform requires a fee but did not quote one")
    hotkey_ss58 = handle.hotkey_ss58
    chain_endpoint = getattr(args, "chain_endpoint", None) or None
    preflight_payment_signer(
        live_wallet=wallet,
        hotkey_ss58=hotkey_ss58,
        subtensor_network=subtensor_network,
        chain_endpoint=chain_endpoint,
    )
    confirm_payment(
        amount_rao=amount_rao,
        dest_address=send_address,
        hotkey_ss58=hotkey_ss58,
        coldkey_name=coldkey_name,
        skip=args.yes,
    )
    print(f"submitting payment on subtensor={subtensor_network}...", file=sys.stderr)
    receipt = submit_eval_payment(
        live_wallet=wallet,
        subtensor_network=subtensor_network,
        amount_rao=amount_rao,
        dest_address=send_address,
        chain_endpoint=chain_endpoint,
    )
    print(
        f"payment finalised: block={receipt.block_number} "
        f"ext_idx={receipt.extrinsic_index}",
        file=sys.stderr,
    )
    return receipt


def _recovery_hint(receipt: PaymentReceipt) -> str:
    return (
        "\nupload could not be confirmed after payment. The fee is not spent "
        "until an upload is accepted. Within 24 hours, rerunning upload with the "
        "same archive reuses the saved proof; for another archive, or if the "
        "saved proof is gone, rerun with:\n"
        f"  --payment-block-hash {receipt.block_hash} "
        f"--payment-block-number {receipt.block_number} "
        f"--payment-extrinsic-index {receipt.extrinsic_index}\n"
        "and no new transfer is sent."
    )


def _upload(args: argparse.Namespace, archive: Path) -> int:
    recovery = _recovery_receipt_from_args(args)
    with archive.open("rb") as source:
        raw = source.read(MAX_ARCHIVE_BYTES + 1)
    if not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise GeneratorToolError(f"source archive must be 1..{MAX_ARCHIVE_BYTES} bytes")
    info = inspect_archive(archive, explicit=args.challenge_bin)
    digest = hashlib.sha256(raw).hexdigest()
    if info.get("sha256") != digest:
        raise GeneratorToolError("archive check reported a different digest")
    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not args.yes,
    )
    handle, wallet = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
    network = resolve_network(args.network)
    pending = {
        "network": network.name,
        "hotkey": handle.hotkey_ss58,
        "name": PENDING_PAYMENT_NAME,
        "sha256": digest,
        "bucket": GENERATOR_PAYMENTS_BUCKET,
    }
    with ApiClient(base_url=network.api_url) as client:
        receipt = recovery
        if receipt is None:
            # A finalized fee saved by an earlier attempt at this exact archive
            # is reused instead of paying again (a lost response returns the
            # submission that proof already funded).
            receipt = load_pending_payment(**pending)
            if receipt is not None:
                print(
                    "found a saved finalized payment for this exact archive; "
                    "no new transfer will be sent",
                    file=sys.stderr,
                )
        if receipt is None:
            # Read the terms before paying anything. An older Platform has no
            # terms endpoint and never charges for generator uploads.
            terms = client.get_generator_upload_terms(
                hotkey=handle.hotkey_ss58, source_sha256=digest
            )
            if terms is not None:
                blockers = terms.get("blockers") or []
                if blockers:
                    raise MinerCliError(
                        "platform would refuse this upload"
                        + (
                            " (no payment was sent)"
                            if terms.get("payment_required")
                            else ""
                        )
                        + ": "
                        + "; ".join(str(b) for b in blockers)
                    )
                if terms.get("payment_required"):
                    receipt = _pay_fee(
                        args=args,
                        terms=terms,
                        handle=handle,
                        wallet=wallet,
                        coldkey_name=coldkey_name,
                        subtensor_network=network.subtensor_network,
                    )
                    # Saved before any upload attempt, so a crash or a lost
                    # response never strands the proof.
                    if not save_pending_payment(**pending, payment=receipt):
                        print(
                            "warning: could not save the finalized payment proof "
                            "locally; keep the printed proof if upload fails",
                            file=sys.stderr,
                        )
        nonce = str(uuid4())
        timestamp = int(time.time())
        signature = wallet.hotkey.sign(
            build_source_payload(
                hotkey_ss58=handle.hotkey_ss58,
                source_sha256=digest,
                signature_timestamp=timestamp,
                nonce=nonce,
            )
        ).hex()
        body: dict[str, object] = {
            "hotkey": handle.hotkey_ss58,
            "source_sha256": digest,
            "source_base64": base64.b64encode(raw).decode("ascii"),
            "signature": signature,
            "signature_timestamp": timestamp,
            "signature_nonce": nonce,
        }
        if receipt is not None:
            body.update(
                payment_block_hash=receipt.block_hash,
                payment_block_number=receipt.block_number,
                payment_extrinsic_index=receipt.extrinsic_index,
            )
        try:
            result = client.post_generator_submission(body)
        except BaseException:
            # Any failure after paying, including a malformed response or an
            # interrupt, must leave the miner the proof.
            if receipt is not None:
                print(_recovery_hint(receipt), file=sys.stderr)
            raise
    if receipt is not None:
        clear_pending_payment(**pending, payment=receipt)
    print(result["submission_id"])
    superseded = result.get("superseded") or []
    print(
        f"generator {result['status']}: {info.get('files')} files, module "
        f"{info.get('module')}, sha256 {result['source_sha256']}"
        + (
            f"; superseded {len(superseded)} earlier generator(s)" if superseded else ""
        ),
        file=sys.stderr,
    )
    return 0


def run(args: argparse.Namespace) -> int:
    command = args.generator_command

    if command == "board":
        network = resolve_network(args.network)
        with ApiClient(base_url=network.api_url) as client:
            print(json.dumps(client.get_generator_board(), indent=2))
        return 0

    if command == "status":
        hotkey = _resolve_hotkey(args)
        network = resolve_network(args.network)
        with ApiClient(base_url=network.api_url) as client:
            print(
                json.dumps(client.list_generator_submissions(hotkey=hotkey), indent=2)
            )
        return 0

    if command == "rehearse":
        hotkey = _resolve_hotkey(args)
        tool_args = [
            "rehearse",
            "-dir",
            str(args.dir.resolve()),
            "-hotkey",
            hotkey,
            "-seeds",
            str(args.seeds),
            "-cases",
            str(args.cases),
            "-sandbox",
            args.sandbox,
        ]
        if args.out_dir:
            tool_args += ["-out-dir", str(args.out_dir.resolve())]
        return run_tool(tool_args, explicit=args.challenge_bin).returncode

    if command == "package":
        package_dir(args.dir, args.out, explicit=args.challenge_bin)
        return 0

    if command == "upload":
        if (args.archive is None) == (args.dir is None):
            raise GeneratorToolError("pass exactly one of ARCHIVE or --dir DIR")
        if args.dir is None:
            return _upload(args, args.archive)
        with tempfile.TemporaryDirectory(prefix="ditto-generator-") as tmp:
            archive = Path(tmp) / "generator.tar.gz"
            package_dir(args.dir, archive, explicit=args.challenge_bin)
            return _upload(args, archive)

    raise MinerCliError(f"unknown generator command {command!r}")
