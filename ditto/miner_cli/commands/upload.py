"""``ditto upload``: full submission flow.

Glues every other module together. Walk:

1. Load wallet (coldkey + hotkey)
2. Run tar pre-flight; abort on any real check failing
3. Sign ``f"{hotkey}:{sha256}"``
4. POST /upload/check; abort on a definitive rejection
5. Verify the selected coldkey owns the hotkey on chain
6. GET /upload/eval-pricing for the current fee + destination address
7. Show payment preview + interactive confirm (skipped by --yes)
8. Submit Balances.transfer_keep_alive extrinsic via raw bittensor SDK
9. POST /upload/agent with tar + payment proof
10. Print returned agent_id to stdout; print poll hint to stderr

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
from ditto.miner_cli.payment import preflight_payment_signer, submit_eval_payment
from ditto.miner_cli.signing import sign_upload_payload
from ditto.miner_cli.tar_validator import run_preflight
from ditto.miner_cli.wallet import load_wallet

logger = logging.getLogger(__name__)


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    """Register the ``upload`` subparser on the top-level argparse layout.

    ``parents`` carries the shared top-level flags (``--network``,
    ``--subtensor.chain_endpoint``, ``--verbose``) so they accept the
    position after the subcommand as well as before it.
    """
    parser = subparsers.add_parser(
        "upload",
        help="Submit an agent harness tarball + payment to the Ditto API.",
        description=(
            "Run the full upload flow: pre-flight, sign, pre-pay check, "
            "verify wallet ownership, fetch fee, confirm, pay, post tarball, "
            "return agent_id."
        ),
        parents=parents or [],
    )
    parser.add_argument(
        "--path",
        "--tar-path",
        dest="tar_path",
        type=Path,
        required=True,
        help=(
            "Path to the gzipped tarball to upload. Flag aliases: --path / --tar-path."
        ),
    )
    parser.add_argument(
        "--name",
        required=True,
        help="Display name for the agent (1-64 chars). Stored in agents.name.",
    )
    parser.add_argument(
        "--wallet.name",
        "--coldkey",
        dest="coldkey_name",
        default=os.environ.get("WALLET_NAME"),
        help=(
            "Coldkey wallet name. Required (flag or WALLET_NAME env). "
            "Matches the bittensor SDK's --wallet.name; --coldkey is a "
            "shorter alias."
        ),
    )
    parser.add_argument(
        "--wallet.hotkey",
        "--hotkey",
        dest="hotkey_name",
        default=os.environ.get("HOTKEY_NAME"),
        help=(
            "Hotkey name within the coldkey wallet. Required (flag or "
            "HOTKEY_NAME env). Matches the bittensor SDK's "
            "--wallet.hotkey; --hotkey is a shorter alias."
        ),
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
            "error: --wallet.name and --wallet.hotkey are required "
            "(or set WALLET_NAME / HOTKEY_NAME).",
            file=sys.stderr,
        )
        return 1

    network = resolve_network(args.network)

    try:
        return _run_upload(
            args,
            network_api_url=network.api_url,
            subtensor_network=network.subtensor_network,
            chain_endpoint=getattr(args, "chain_endpoint", None) or None,
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
    chain_endpoint: str | None = None,
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

        # Step 5: verify the payment coldkey owns the claimed hotkey. This is
        # intentionally before pricing/confirmation and is never bypassed by
        # --yes: the API enforces the same Owner record at payment time.
        preflight_payment_signer(
            live_wallet=live_wallet,
            hotkey_ss58=handle.hotkey_ss58,
            subtensor_network=subtensor_network,
            chain_endpoint=chain_endpoint,
        )

        # Step 6: fetch current pricing
        pricing = client.get_eval_pricing()

        # Step 7: confirm payment
        confirm_payment(
            amount_rao=pricing.amount_rao,
            dest_address=pricing.send_address,
            hotkey_ss58=handle.hotkey_ss58,
            coldkey_name=handle.coldkey_name,
            skip=args.yes,
        )

        # Step 8: submit chain payment
        print(
            f"submitting payment on subtensor={subtensor_network}...",
            file=sys.stderr,
        )
        receipt = submit_eval_payment(
            live_wallet=live_wallet,
            subtensor_network=subtensor_network,
            amount_rao=pricing.amount_rao,
            dest_address=pricing.send_address,
            chain_endpoint=chain_endpoint,
        )
        print(
            f"payment finalised: block={receipt.block_number} "
            f"ext_idx={receipt.extrinsic_index}",
            file=sys.stderr,
        )

        # Step 9: post tar + payment proof
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

    # Step 10: print agent_id to stdout, hint to stderr
    print(result.agent_id)
    print(
        f"\nupload succeeded. poll status with:\n  ditto status {result.agent_id}",
        file=sys.stderr,
    )
    return 0
