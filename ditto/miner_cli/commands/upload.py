"""``ditto upload``: full 10-step submission flow.

Glues every other module together. Walk:

1. Load wallet (coldkey + hotkey)
2. Run tar pre-flight; abort on any real check failing
3. Sign ``f"{hotkey}:{sha256}"``
4. POST /upload/check; abort on a definitive rejection
5. GET /upload/eval-pricing for the current fee + destination address
6. Show payment preview + interactive confirm (skipped by --yes)
7. Submit Balances.transfer_keep_alive extrinsic via raw bittensor SDK
8. POST /upload/agent with tar + payment proof
9. Print returned agent_id to stdout; print poll hint to stderr

Exit codes:
- 0 success (agent_id printed to stdout)
- 1 generic error (pre-flight failed, sig failed, API rejected, chain failure)
- 2 payment cancelled at confirm prompt
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from ditto.api_models import UploadCheckRequest
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands.verify import _print_result
from ditto.miner_cli.confirm import confirm_payment
from ditto.miner_cli.errors import (
    ApiResponseError,
    MinerCliError,
    PaymentCancelledError,
    PaymentFinalizationTimeoutError,
    PaymentSubmissionError,
    PreCheckRejectedError,
    TarStructureError,
    UploadAgentRejectedError,
    WalletNotFoundError,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.payment import submit_eval_payment
from ditto.miner_cli.signing import sign_upload_payload
from ditto.miner_cli.tar_validator import run_preflight
from ditto.miner_cli.wallet import load_wallet

logger = logging.getLogger(__name__)


def add_subparser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register the ``upload`` subparser on the top-level argparse layout."""
    parser = subparsers.add_parser(
        "upload",
        help="Submit an agent harness tarball + payment to the Ditto API.",
        description=(
            "Run the full 10-step upload flow: pre-flight, sign, pre-pay "
            "check, fetch fee, confirm, pay, post tarball, return agent_id."
        ),
    )
    parser.add_argument(
        "tar_path",
        type=Path,
        help="Path to the gzipped tarball to upload.",
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Display name for the agent (1-64 chars). Stored in agents.name.",
    )
    parser.add_argument(
        "--coldkey-name",
        default=os.environ.get("DITTO_COLDKEY_NAME"),
        help="Coldkey name. Required (flag or DITTO_COLDKEY_NAME env).",
    )
    parser.add_argument(
        "--hotkey-name",
        default=os.environ.get("DITTO_HOTKEY_NAME"),
        help="Hotkey name. Required (flag or DITTO_HOTKEY_NAME env).",
    )
    parser.add_argument(
        "-y",
        "--yes",
        dest="yes",
        action="store_true",
        help="Skip interactive payment confirmation. For scripted use.",
    )
    parser.set_defaults(func=run)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute the upload subcommand and return an exit code."""
    if not args.coldkey_name or not args.hotkey_name:
        print(
            "error: --coldkey-name and --hotkey-name are required "
            "(or set DITTO_COLDKEY_NAME / DITTO_HOTKEY_NAME).",
            file=sys.stderr,
        )
        return 1

    network = resolve_network(args.network)

    try:
        return _run_upload(
            args,
            network_api_url=network.api_url,
            subtensor_network=network.subtensor_network,
        )
    except PaymentCancelledError as e:
        print(f"cancelled: {e}", file=sys.stderr)
        return 2
    except (
        TarStructureError,
        WalletNotFoundError,
        PreCheckRejectedError,
        UploadAgentRejectedError,
        PaymentSubmissionError,
        PaymentFinalizationTimeoutError,
        ApiResponseError,
        MinerCliError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _run_upload(
    args: argparse.Namespace,
    *,
    network_api_url: str,
    subtensor_network: str,
) -> int:
    # Step 1: load wallet
    handle, live_wallet = load_wallet(
        coldkey_name=args.coldkey_name, hotkey_name=args.hotkey_name
    )

    # Step 2: pre-flight (raises TarStructureError on missing file)
    print(f"running pre-flight on {args.tar_path}...", file=sys.stderr)
    preflight = run_preflight(args.tar_path)
    _print_result(preflight)
    if not preflight.passed:
        raise TarStructureError("pre-flight failed; see report above")

    # Step 3: sign
    signature_hex = sign_upload_payload(
        handle=handle, live_wallet=live_wallet, sha256_hex=preflight.sha256
    )

    with ApiClient(base_url=network_api_url) as client:
        # Step 4: pre-payment check
        check_response = client.post_upload_check(
            UploadCheckRequest(
                hotkey=handle.hotkey_ss58,
                sha256=preflight.sha256,
                file_size_bytes=preflight.file_size_bytes,
                signature=signature_hex,
            )
        )
        if not check_response.ok:
            for code, msg in zip(
                check_response.error_codes, check_response.messages, strict=True
            ):
                print(f"  pre-check rejection {code}: {msg}", file=sys.stderr)
            raise PreCheckRejectedError(
                f"pre-check rejected: codes={check_response.error_codes}"
            )

        # Step 5: fetch current pricing
        pricing = client.get_eval_pricing()

        # Step 6: confirm payment
        confirm_payment(
            amount_rao=pricing.amount_rao,
            dest_address=pricing.send_address,
            hotkey_ss58=handle.hotkey_ss58,
            coldkey_name=handle.coldkey_name,
            skip=args.yes,
        )

        # Step 7: submit chain payment
        print(
            f"submitting payment on subtensor={subtensor_network}...",
            file=sys.stderr,
        )
        receipt = submit_eval_payment(
            live_wallet=live_wallet,
            subtensor_network=subtensor_network,
            amount_rao=pricing.amount_rao,
            dest_address=pricing.send_address,
        )
        print(
            f"payment finalised: block={receipt.block_number} "
            f"ext_idx={receipt.extrinsic_index}",
            file=sys.stderr,
        )

        # Step 8: post tar + payment proof
        print("uploading tarball...", file=sys.stderr)
        try:
            with args.tar_path.open("rb") as tar_fh:
                result = client.post_upload_agent(
                    agent_tar=tar_fh,
                    agent_tar_filename=args.tar_path.name,
                    hotkey=handle.hotkey_ss58,
                    sha256=preflight.sha256,
                    name=args.name,
                    signature=signature_hex,
                    payment=receipt,
                )
        except ApiResponseError:
            # Money is on chain. Any post-payment API failure (server
            # rejection OR transport error like connect-refused / timeout)
            # must surface the proof so the miner can take it to support.
            # Catching the ApiResponseError base covers UploadAgentRejectedError
            # subclasses AND the bare transport-wrapped errors raised by
            # api_client._request.
            print(
                f"\nupload failed after payment. "
                f"Keep this proof for support:\n"
                f"  block_hash:       {receipt.block_hash}\n"
                f"  block_number:     {receipt.block_number}\n"
                f"  extrinsic_index:  {receipt.extrinsic_index}",
                file=sys.stderr,
            )
            raise

    # Step 9: print agent_id to stdout, hint to stderr
    print(result.agent_id)
    print(
        f"\nupload succeeded. poll status with:\n  ditto status {result.agent_id}",
        file=sys.stderr,
    )
    return 0
