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
import re
import sys
import time
from pathlib import Path

from ditto.api_models import UploadAgentResponse, UploadCheckRequest
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
    TransientApiError,
    UploadAgentRejectedError,
    WalletNotFoundError,
)
from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.payment import preflight_payment_signer, submit_eval_payment
from ditto.miner_cli.preferences import (
    clear_pending_payment,
    load_agent_name,
    load_pending_payment,
    save_agent_name,
    save_pending_payment,
)
from ditto.miner_cli.signing import sign_upload_payload
from ditto.miner_cli.tar_validator import run_preflight
from ditto.miner_cli.wallet import load_wallet

logger = logging.getLogger(__name__)

_BLOCK_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_UPLOAD_RETRY_DELAYS_S = (1.0, 2.0, 4.0, 8.0)


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
        default=os.environ.get("DITTO_AGENT_NAME"),
        help=(
            "Stable agent name (1-64 chars). Reuse it for v2, v3, and later "
            "uploads. After success it becomes this hotkey's local default; "
            "pass --name again to change it. Env: DITTO_AGENT_NAME."
        ),
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
    recovery = parser.add_argument_group(
        "payment recovery",
        "Reuse a finalized payment after an upload transport/server failure. "
        "All three fields are required together; no new transfer is submitted.",
    )
    recovery.add_argument("--payment-block-hash")
    recovery.add_argument("--payment-block-number", type=int)
    recovery.add_argument("--payment-extrinsic-index", type=int)
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
            network_name=network.name,
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
    network_name: str,
    network_api_url: str,
    subtensor_network: str,
    chain_endpoint: str | None = None,
) -> int:
    explicit_recovery_receipt = _recovery_receipt_from_args(args)

    # Step 1: load wallet
    handle, live_wallet = load_wallet(
        coldkey_name=args.coldkey_name, hotkey_name=args.hotkey_name
    )

    agent_name = args.name
    if agent_name is None:
        agent_name = load_agent_name(network=network_name, hotkey=handle.hotkey_ss58)
        if agent_name is not None:
            print(
                f"using saved agent name: {agent_name} (override with --name)",
                file=sys.stderr,
            )
        elif sys.stdin.isatty():
            agent_name = input("Agent name (reuse this for future versions): ").strip()
        else:
            raise MinerCliError(
                "agent name required for the first upload; pass --name. "
                "Ditto will remember it for this hotkey after success."
            )
    agent_name = agent_name.strip()
    if not 1 <= len(agent_name) <= 64:
        raise MinerCliError("agent name must be 1-64 characters")

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

    # A finalized receipt is written locally before the first upload attempt.
    # On a later identical command, reuse it automatically instead of sending a
    # second transfer. The key includes the local tar digest; no tar bytes, paths,
    # source, or remote artifact access are stored.
    receipt = explicit_recovery_receipt
    receipt_source = "explicit" if receipt is not None else None
    if receipt is None:
        receipt = load_pending_payment(
            network=network_name,
            hotkey=handle.hotkey_ss58,
            name=agent_name,
            sha256=preflight.sha256,
        )
        if receipt is not None:
            receipt_source = "saved"

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

        if receipt is None:
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
            if not save_pending_payment(
                network=network_name,
                hotkey=handle.hotkey_ss58,
                name=agent_name,
                sha256=preflight.sha256,
                payment=receipt,
            ):
                print(
                    "warning: could not save the finalized payment proof locally; "
                    "keep the printed proof if upload fails",
                    file=sys.stderr,
                )
        else:
            print(
                (
                    "found a saved finalized payment for this exact submission; "
                    "no new transfer will be sent"
                    if receipt_source == "saved"
                    else "reusing finalized payment proof; no new transfer will be sent"
                ),
                file=sys.stderr,
            )

        # Step 9: post tar + payment proof
        print("uploading tarball...", file=sys.stderr)
        try:
            result = _post_upload_with_retries(
                client=client,
                tar_path=args.tar_path,
                hotkey=handle.hotkey_ss58,
                sha256=preflight.sha256,
                name=agent_name,
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
    if not clear_pending_payment(
        network=network_name,
        hotkey=handle.hotkey_ss58,
        name=agent_name,
        sha256=preflight.sha256,
        payment=receipt,
    ):
        print(
            "warning: upload succeeded but the saved payment proof could not be "
            "cleared; the server will still return this same agent on an exact retry",
            file=sys.stderr,
        )
    saved_name = save_agent_name(
        network=network_name, hotkey=handle.hotkey_ss58, name=agent_name
    )
    print(
        f"\nupload succeeded: {agent_name} · submission v{result.version}\n"
        + (
            "saved as this hotkey's local default; override with --name\n"
            if saved_name
            else "warning: could not save the local agent-name default\n"
        )
        + f"poll status with:\n  ditto status {result.agent_id}",
        file=sys.stderr,
    )
    return 0


def _recovery_receipt_from_args(args: argparse.Namespace) -> PaymentReceipt | None:
    """Parse the all-or-none finalized-payment recovery flags."""
    block_hash = getattr(args, "payment_block_hash", None)
    block_number = getattr(args, "payment_block_number", None)
    extrinsic_index = getattr(args, "payment_extrinsic_index", None)
    supplied = (
        block_hash is not None,
        block_number is not None,
        extrinsic_index is not None,
    )
    if any(supplied) and not all(supplied):
        raise MinerCliError(
            "payment recovery requires --payment-block-hash, "
            "--payment-block-number, and --payment-extrinsic-index together"
        )
    if not any(supplied):
        return None
    if not isinstance(block_hash, str) or not _BLOCK_HASH_RE.fullmatch(block_hash):
        raise MinerCliError("--payment-block-hash must be 0x plus 64 hex characters")
    if not isinstance(block_number, int) or block_number < 1:
        raise MinerCliError("--payment-block-number must be at least 1")
    if not isinstance(extrinsic_index, int) or extrinsic_index < 0:
        raise MinerCliError("--payment-extrinsic-index must be at least 0")
    return PaymentReceipt(
        block_hash=block_hash,
        block_number=block_number,
        extrinsic_index=extrinsic_index,
    )


def _post_upload_with_retries(
    *,
    client: ApiClient,
    tar_path: Path,
    hotkey: str,
    sha256: str,
    name: str,
    signature: str,
    payment: PaymentReceipt,
) -> UploadAgentResponse:
    """Retry only transient post-payment failures with the same proof."""
    for attempt in range(len(_UPLOAD_RETRY_DELAYS_S) + 1):
        try:
            with tar_path.open("rb") as tar_fh:
                return client.post_upload_agent(
                    agent_tar=tar_fh,
                    agent_tar_filename=tar_path.name,
                    hotkey=hotkey,
                    sha256=sha256,
                    name=name,
                    signature=signature,
                    payment=payment,
                )
        except TransientApiError:
            if attempt == len(_UPLOAD_RETRY_DELAYS_S):
                raise
            delay = _UPLOAD_RETRY_DELAYS_S[attempt]
            print(
                f"upload endpoint temporarily unavailable; retrying in {delay:g}s "
                f"({attempt + 2}/{len(_UPLOAD_RETRY_DELAYS_S) + 1})...",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable upload retry loop")
