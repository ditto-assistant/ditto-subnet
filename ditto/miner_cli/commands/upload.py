"""``ditto upload``: full submission flow.

Glues every other module together. Walk:

1. Load wallet (coldkey + hotkey)
2. Run tar pre-flight; abort on any real check failing
3. Sign ``f"{hotkey}:{sha256}"``
4. POST /upload/check; abort on a definitive rejection. When the sole
   rejection is 1101 (hotkey not registered), offer to recycle the live
   registration cost, then re-check and continue
5. Verify the selected coldkey owns the hotkey on chain
6. Use the TAO fee + destination atomically bound to the admission reservation
   (fall back to GET /upload/eval-pricing during an older-server rollout)
7. Show payment preview + interactive confirm (skipped by --yes)
8. Submit Balances.transfer_keep_alive extrinsic via raw bittensor SDK
9. POST /upload/agent with tar + payment proof
10. Print returned agent_id to stdout; print poll hint to stderr

Exit codes:
- 0 success (agent_id printed to stdout)
- 1 generic error (pre-flight failed, sig failed, API rejected, chain failure)
- 2 payment or registration cancelled at a confirm prompt
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from ditto.api_models import (
    EvalPricingResponse,
    UploadAgentResponse,
    UploadCheckRequest,
    UploadCheckResponse,
)
from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands.verify import _print_result
from ditto.miner_cli.confirm import confirm_payment, confirm_registration
from ditto.miner_cli.errors import (
    ApiResponseError,
    MinerCliError,
    PaymentAmountMismatchError,
    PaymentCancelledError,
    PaymentFinalizationTimeoutError,
    PaymentRecoveryExpiredError,
    PaymentSubmissionError,
    PreCheckRejectedError,
    RegistrationCancelledError,
    RegistrationNotNeededError,
    RegistrationOutcomeUnknownError,
    RegistrationSubmissionError,
    SubmissionCooldownError,
    TarStructureError,
    TransientApiError,
    UploadAgentRejectedError,
    WalletNotFoundError,
)
from ditto.miner_cli.models import (
    PaymentReceipt,
    PendingUploadPayment,
    WalletHandle,
)
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.payment import preflight_payment_signer, submit_eval_payment
from ditto.miner_cli.preferences import (
    AgentWalletIdentity,
    clear_pending_payment,
    load_agent_name,
    load_pending_payment,
    load_pending_payments_for_hotkey,
    load_previous_agent_wallet,
    save_agent_name,
    save_agent_wallet,
    save_pending_payment,
)
from ditto.miner_cli.registration import quote_registration, submit_registration
from ditto.miner_cli.signing import sign_upload_payload
from ditto.miner_cli.tar_validator import run_preflight
from ditto.miner_cli.wallet import load_wallet

if TYPE_CHECKING:
    import bittensor

logger = logging.getLogger(__name__)

_BLOCK_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_UPLOAD_RETRY_DELAYS_S = (2.0, 4.0, 8.0, 16.0, 30.0, 30.0)

# How long to keep re-checking after a finalized registration. The platform
# answers 1101 from a cached metagraph snapshot rather than from the chain
# (ditto.chain.client.ChainClient.get_recent_neurons), so a registration that
# is already final on chain stays invisible to /upload/check for a while. That
# snapshot's refresh period is not a contract the CLI can read, so this polls
# rather than sleeping a fixed guess. ~4 minutes total.
_REGISTRATION_VISIBILITY_DELAYS_S = (5.0, 10.0, 15.0, 20.0, 30.0, 30.0, 30.0, 30.0)

# Mirrors ditto.api_server.endpoints.upload.ERROR_CODE_HOTKEY_NOT_REGISTERED.
# The only pre-check rejection the CLI can resolve on the miner's behalf.
_ERROR_CODE_HOTKEY_NOT_REGISTERED = 1101


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
    parser.add_argument(
        "--register",
        dest="register",
        action="store_true",
        help=(
            "Pre-authorize recycling the live registration cost if this "
            "hotkey is not yet registered on the subnet, skipping the "
            "registration prompt. For scripted use. Deliberately NOT implied "
            "by --yes: --yes covers the platform-quoted eval fee, while the "
            "recycle amount is a separate chain-quoted cost with no ceiling. "
            "Without this flag an interactive run still offers to register."
        ),
    )
    parser.add_argument(
        "--no-register",
        dest="no_register",
        action="store_true",
        help=(
            "Never offer to register. An unregistered hotkey fails the "
            "pre-check exactly as before, printing the btcli command."
        ),
    )
    parser.add_argument(
        "--allow-identical-rescore",
        dest="allow_identical_rescore",
        action="store_true",
        help=(
            "Deliberately buy another independently seeded run of source that "
            "is byte-identical to one you already submitted. Off by default so "
            "an accidental duplicate cannot spend TAO."
        ),
    )
    recovery = parser.add_argument_group(
        "payment recovery",
        "Reuse a finalized payment after an upload transport/server failure. "
        "All three fields are required together; no new transfer is submitted.",
    )
    recovery.add_argument("--payment-block-hash")
    recovery.add_argument("--payment-block-number", type=int)
    recovery.add_argument("--payment-extrinsic-index", type=int)
    recovery.add_argument(
        "--pay-again",
        action="store_true",
        help=(
            "If the platform definitively rejects a saved receipt for amount "
            "mismatch or expiry, retire it and continue to a newly confirmed "
            "payment. Never causes a second transfer for other failures."
        ),
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
            network_name=network.name,
            network_api_url=network.api_url,
            subtensor_network=network.subtensor_network,
            chain_endpoint=getattr(args, "chain_endpoint", None) or None,
        )
    except (PaymentCancelledError, RegistrationCancelledError) as e:
        print(f"cancelled: {e}", file=sys.stderr)
        return 2
    except (
        TarStructureError,
        WalletNotFoundError,
        PreCheckRejectedError,
        RegistrationSubmissionError,
        RegistrationNotNeededError,
        RegistrationOutcomeUnknownError,
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

    allow_identical_rescore = bool(getattr(args, "allow_identical_rescore", False))

    previous_wallet = load_previous_agent_wallet(
        network=network_name,
        name=agent_name,
        excluding_hotkey=handle.hotkey_ss58,
    )
    if previous_wallet is not None:
        _offer_owner_link(
            args=args,
            agent_name=agent_name,
            previous_wallet=previous_wallet,
            current_hotkey=handle.hotkey_ss58,
            network_api_url=network_api_url,
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

    # A finalized receipt is written locally before the first upload attempt.
    # On a later identical command, reuse it automatically instead of sending a
    # second transfer. The key includes the local tar digest; no tar bytes, paths,
    # source, or remote artifact access are stored.
    receipt = explicit_recovery_receipt
    reassigned_payment = None
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
        else:
            candidates = load_pending_payments_for_hotkey(
                network=network_name,
                hotkey=handle.hotkey_ss58,
            )
            if len(candidates) > 1:
                raise MinerCliError(
                    "multiple unconsumed payments are saved for this hotkey; "
                    "rerun with the intended --payment-block-* proof flags"
                )
            if candidates:
                reassigned_payment = candidates[0]
                receipt = reassigned_payment.payment
                receipt_source = "saved_reassigned"

    with ApiClient(base_url=network_api_url) as client:
        # Step 4: pre-payment check
        def _check(candidate_receipt: PaymentReceipt | None):
            return client.post_upload_check(
                UploadCheckRequest(
                    hotkey=handle.hotkey_ss58,
                    sha256=preflight.sha256,
                    file_size_bytes=preflight.file_size_bytes,
                    signature=signature_hex,
                    allow_identical_rescore=allow_identical_rescore,
                    reserve_submission_slot=True,
                    payment_block_hash=(
                        candidate_receipt.block_hash if candidate_receipt else None
                    ),
                    payment_block_number=(
                        candidate_receipt.block_number if candidate_receipt else None
                    ),
                    payment_extrinsic_index=(
                        candidate_receipt.extrinsic_index if candidate_receipt else None
                    ),
                )
            )

        try:
            check_response = _check(receipt)
        except (PaymentAmountMismatchError, PaymentRecoveryExpiredError) as error:
            if receipt is None:
                raise
            _authorize_replacement_payment(args=args, error=error, receipt=receipt)
            check_response = _check(None)
            if check_response.ok and check_response.admission_token is not None:
                _retire_rejected_saved_payment(
                    receipt_source=receipt_source,
                    reassigned_payment=reassigned_payment,
                    hotkey=handle.hotkey_ss58,
                    name=agent_name,
                    sha256=preflight.sha256,
                    network=network_name,
                    payment=receipt,
                )
                receipt = None
                receipt_source = None
                reassigned_payment = None
        if not check_response.ok:
            _print_rejections(check_response)
            if _registration_would_unblock(args, check_response.error_codes):
                registered_uid = _register_hotkey(
                    args=args,
                    handle=handle,
                    live_wallet=live_wallet,
                    subtensor_network=subtensor_network,
                    chain_endpoint=chain_endpoint,
                    check_response=check_response,
                )
                # The platform resolves registration from a cached metagraph
                # snapshot, not from the chain directly, so a finalized
                # registration is not visible to /upload/check immediately.
                check_response = _await_platform_registration(
                    check=lambda: _check(receipt),
                    hotkey_ss58=handle.hotkey_ss58,
                    registered_uid=registered_uid,
                )
                if not check_response.ok:
                    _print_rejections(check_response)
            if not check_response.ok:
                if _ERROR_CODE_HOTKEY_NOT_REGISTERED in check_response.error_codes:
                    # Reached when registration was declined by flag, or when
                    # another rejection rode alongside 1101. Either way the
                    # miner still has to register eventually, so hand them the
                    # command rather than only the code.
                    print(
                        f"\nto register this hotkey:\n"
                        f"{_btcli_register_hint(args, subtensor_network)}",
                        file=sys.stderr,
                    )
                raise PreCheckRejectedError(
                    f"pre-check rejected: codes={check_response.error_codes}"
                )
        if check_response.admission_token is None:
            raise PreCheckRejectedError(
                "platform did not reserve a pre-payment submission slot; "
                "no payment was sent. The platform rollout may still be in "
                "progress; retry shortly."
            )
        admission_token = check_response.admission_token
        if receipt is not None and check_response.payment_required:
            raise PreCheckRejectedError(
                "platform did not recognize the finalized payment as reusable; "
                "no new payment was sent"
            )

        if reassigned_payment is not None:
            assert receipt is not None
            if not save_pending_payment(
                network=network_name,
                hotkey=handle.hotkey_ss58,
                name=agent_name,
                sha256=preflight.sha256,
                payment=receipt,
            ):
                print(
                    "warning: platform reassigned the payment but the new local "
                    "recovery record could not be saved; keep the printed proof",
                    file=sys.stderr,
                )
            else:
                clear_pending_payment(
                    network=reassigned_payment.network,
                    hotkey=reassigned_payment.hotkey,
                    name=reassigned_payment.name,
                    sha256=reassigned_payment.sha256,
                    payment=reassigned_payment.payment,
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
            # Step 6: use the fee atomically bound to this reservation. Fall
            # back to the legacy pricing endpoint while older servers roll out.
            if (
                check_response.payment_amount_rao is not None
                and check_response.payment_send_address is not None
            ):
                pricing = EvalPricingResponse(
                    amount_rao=check_response.payment_amount_rao,
                    send_address=check_response.payment_send_address,
                )
            else:
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
                    else (
                        "found an unconsumed finalized payment for this hotkey; "
                        f"applying it to {agent_name} and the new archive. "
                        "The prior archive reservation is cancelled and no new "
                        "transfer will be sent"
                        if receipt_source == "saved_reassigned"
                        else (
                            "reusing finalized payment proof; no new transfer "
                            "will be sent"
                        )
                    )
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
                admission_token=admission_token,
                allow_identical_rescore=allow_identical_rescore,
            )
        except ApiResponseError:
            # Money is on chain. Any post-payment API failure (server
            # rejection OR transport error like connect-refused / timeout)
            # must surface the proof so the miner can take it to support.
            # Catching the ApiResponseError base covers UploadAgentRejectedError
            # subclasses AND the bare transport-wrapped errors raised by
            # api_client._request.
            print(
                f"\nupload could not be confirmed after payment. "
                f"Your finalized payment was saved and can be reused.\n"
                f"Rerun upload with this hotkey using either the same archive or "
                f"a replacement; the CLI will reuse this proof and will not send "
                f"another transfer.\n"
                f"If the saved proof is unavailable, rerun with:\n"
                f"  --payment-block-hash {receipt.block_hash} "
                f"--payment-block-number {receipt.block_number} "
                f"--payment-extrinsic-index {receipt.extrinsic_index}\n"
                f"Payment proof:\n"
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
    saved_wallet = save_agent_wallet(
        network=network_name,
        name=agent_name,
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        hotkey_ss58=handle.hotkey_ss58,
    )
    if result.payment_disposition == "reusable_credit":
        # The platform returned an EXISTING agent: these bytes were already
        # submitted under this owner, so no new evaluation was purchased and the
        # payment is banked as a credit. Reporting the ordinary success line
        # here would tell the miner they bought a run they did not.
        print(
            f"\nno new submission was created: {agent_name} is byte-identical "
            f"to an artifact you already submitted"
            + (
                f" (agent {result.credit_for_agent_id})"
                if result.credit_for_agent_id
                else ""
            )
            + ".\n"
            "Your payment was NOT spent -- the platform banked it as a reusable "
            "credit, and your next upload of different source will consume it "
            "with no new transfer.\n"
            "To deliberately buy a second, independently seeded run of these "
            "exact bytes, resubmit with --allow-identical-rescore.\n"
            f"poll the existing submission with:\n  ditto status {result.agent_id}",
            file=sys.stderr,
        )
        return 0
    print(
        f"\nupload succeeded: {agent_name} · submission v{result.version}"
        + (
            " (funded by your reusable credit; no new transfer was sent)"
            if result.payment_disposition == "credit_consumed"
            else ""
        )
        + "\n"
        + (
            "saved as this hotkey's local default; override with --name\n"
            if saved_name and saved_wallet
            else "warning: could not save the local agent-name default\n"
        )
        + f"poll status with:\n  ditto status {result.agent_id}",
        file=sys.stderr,
    )
    return 0


def _print_rejections(check_response: UploadCheckResponse) -> None:
    """Print each parallel (code, message) pair from a failed pre-check."""
    for code, msg in zip(
        check_response.error_codes, check_response.messages, strict=True
    ):
        print(f"  pre-check rejection {code}: {msg}", file=sys.stderr)


def _await_platform_registration(
    *,
    check: Callable[[], UploadCheckResponse],
    hotkey_ss58: str,
    registered_uid: int | None,
) -> UploadCheckResponse:
    """Re-check until the platform observes an on-chain registration.

    The chain and the platform do not agree instantly. ``/upload/check``
    resolves registration from Pylon's cached recent-neurons snapshot, so a
    registration that has already finalized on chain still answers 1101 for
    some time afterward. Checking once and giving up sends the miner away
    from a submission that is seconds from working, having already spent the
    recycle amount.

    Only 1101 is waited on. Any other rejection is returned immediately --
    those do not become true by waiting.

    Args:
        check: Zero-arg callable re-running the same ``/upload/check``.
        hotkey_ss58: Hotkey being waited on, for the messages.
        registered_uid: uid from this run's registration, when known.

    Returns:
        The first passing response, or the last non-1101 rejection.

    Raises:
        PreCheckRejectedError: The platform never observed the registration
            within the budget. The message states that registration DID
            succeed so the miner does not pay to register a second time.
    """
    response = check()
    if response.ok or list(response.error_codes) != [_ERROR_CODE_HOTKEY_NOT_REGISTERED]:
        return response

    print(
        "waiting for the platform to observe the registration "
        "(it reads a cached metagraph snapshot, so this lags the chain)...",
        file=sys.stderr,
    )
    for attempt, delay in enumerate(_REGISTRATION_VISIBILITY_DELAYS_S, start=1):
        time.sleep(delay)
        response = check()
        if response.ok:
            print(
                f"platform observed the registration after "
                f"{attempt}/{len(_REGISTRATION_VISIBILITY_DELAYS_S)} checks",
                file=sys.stderr,
            )
            return response
        if list(response.error_codes) != [_ERROR_CODE_HOTKEY_NOT_REGISTERED]:
            return response
        print(
            f"  still not visible; re-checking "
            f"({attempt}/{len(_REGISTRATION_VISIBILITY_DELAYS_S)})",
            file=sys.stderr,
        )

    uid_note = f" as uid {registered_uid}" if registered_uid is not None else ""
    raise PreCheckRejectedError(
        f"hotkey {hotkey_ss58} is registered on chain{uid_note}, but the "
        f"platform still reports it as unregistered after "
        f"{int(sum(_REGISTRATION_VISIBILITY_DELAYS_S))}s.\n"
        "The registration succeeded and the recycled TAO is already spent -- "
        "do NOT register again. Re-run this exact upload command in a few "
        "minutes; it will skip registration and go straight to payment.\n"
        "If it persists, the platform may be bound to a different netuid "
        "than the CLI (env NETUID) or a different network (--network)."
    )


def _resolve_netuid() -> int:
    """Locally configured netuid, matching ``ditto attest``.

    Display only. Never the target of a registration: see
    :func:`_authoritative_netuid`.
    """
    from ditto.miner_cli.commands.attest import DEFAULT_NETUID

    return int(os.environ.get("NETUID", str(DEFAULT_NETUID)))


def _authoritative_netuid(
    *,
    check_response: UploadCheckResponse,
    args: argparse.Namespace,
    subtensor_network: str,
) -> int:
    """Return the netuid the platform itself reports, or refuse to register.

    The 1101 rejection is issued against the netuid the *platform* is bound
    to. Registering against the netuid the *CLI* happens to be configured
    with is only correct while the two agree, and nothing in the flow proves
    they do: when the hotkey is unregistered on both, the already-registered
    guard sees an ordinary unregistered hotkey and waves the burn through
    onto the wrong subnet.

    So the target is taken from the response or not at all. Falling back to
    the local value is precisely the behavior that spends TAO in the wrong
    place, so a server too old to report its binding gets the manual path.

    Raises:
        RegistrationSubmissionError: The response named no netuid, or named
            a subtensor network other than the one this run would sign on.
    """
    manual = _btcli_register_hint(args, subtensor_network)
    if check_response.netuid is None:
        raise RegistrationSubmissionError(
            "the platform did not report which netuid it is bound to, so the "
            "CLI cannot know where to register. Registering against the "
            "locally configured netuid could recycle TAO on a subnet the "
            "platform does not watch. No TAO was recycled.\n"
            f"Register manually once you have confirmed the target:\n{manual}"
        )

    reported_network = check_response.subtensor_network
    if reported_network is not None and reported_network != subtensor_network:
        raise RegistrationSubmissionError(
            f"the platform reads registration from subtensor "
            f"{reported_network!r} but this run would register on "
            f"{subtensor_network!r}. The right netuid on the wrong chain is "
            f"still the wrong target. Re-run with --network matching the "
            f"platform. No TAO was recycled."
        )

    local = _resolve_netuid()
    if check_response.netuid != local:
        # Not fatal: the platform is authoritative and the CLI follows it.
        # Worth saying out loud, because the local value is what every other
        # netuid-taking command in this CLI would have used.
        print(
            f"note: registering on netuid {check_response.netuid} as reported "
            f"by the platform, not the locally configured {local}.",
            file=sys.stderr,
        )
    return check_response.netuid


def _btcli_register_hint(
    args: argparse.Namespace, subtensor_network: str, *, netuid: int | None = None
) -> str:
    """The equivalent btcli command, with this run's wallet already filled in.

    Printed on every path where the CLI will not or cannot register, so a
    miner is never left holding only an error code.
    """
    return (
        f"  btcli subnets register --netuid {netuid or _resolve_netuid()} "
        f"--wallet-name {args.coldkey_name} --hotkey {args.hotkey_name} "
        f"--network {subtensor_network}"
    )


def _registration_would_unblock(
    args: argparse.Namespace, error_codes: list[int]
) -> bool:
    """``True`` when registering is the whole remaining fix.

    Deliberately requires 1101 to be the *only* rejection. A hotkey can be
    unregistered AND have a bad signature, or be inside an owner cooldown;
    registering then recycles real TAO and lands on the same wall. The
    conservative test costs a second upload run in the rare multi-code case
    and cannot burn TAO for nothing.
    """
    if getattr(args, "no_register", False):
        return False
    return list(error_codes) == [_ERROR_CODE_HOTKEY_NOT_REGISTERED]


def _register_hotkey(
    *,
    args: argparse.Namespace,
    handle: WalletHandle,
    live_wallet: bittensor.Wallet,
    subtensor_network: str,
    chain_endpoint: str | None,
    check_response: UploadCheckResponse,
) -> int | None:
    """Quote, confirm, and submit a burned registration, then continue.

    The target comes from the platform's own response, never from local
    configuration. Registering against a locally chosen netuid can recycle
    TAO on a subnet the platform does not watch, and the already-registered
    guard cannot catch it: a hotkey unregistered on both subnets looks
    identical either way. A server that does not report its target is
    treated as unregisterable rather than guessed at.

    Every failure mode ends by printing the equivalent btcli command, so a
    miner the CLI cannot register is never left without the next step.

    Returns:
        The uid assigned by this run's registration, or ``None`` when the
        hotkey was already registered on chain or the uid could not be read.

    Raises:
        RegistrationCancelledError: The miner declined the prompt.
        RegistrationSubmissionError: The platform did not name a target, the
            target disagrees with this run's chain, or quoting failed.
        RegistrationOutcomeUnknownError: Submission left chain state
            ambiguous.
    """
    netuid = _authoritative_netuid(
        check_response=check_response,
        args=args,
        subtensor_network=subtensor_network,
    )
    btcli_hint = _btcli_register_hint(args, subtensor_network, netuid=netuid)

    try:
        quote = quote_registration(
            live_wallet=live_wallet,
            hotkey_ss58=handle.hotkey_ss58,
            coldkey_name=handle.coldkey_name,
            netuid=netuid,
            subtensor_network=subtensor_network,
            chain_endpoint=chain_endpoint,
        )
    except RegistrationNotNeededError:
        # Not an error here. The platform said 1101 and the chain says
        # registered, which is the ordinary state right after registering:
        # the platform's cached snapshot has not caught up yet. Burning again
        # would be the actual mistake. Let the caller wait it out.
        print(
            f"chain already reports {handle.hotkey_ss58} registered on netuid "
            f"{netuid}; no TAO was recycled.",
            file=sys.stderr,
        )
        return None
    except RegistrationSubmissionError as e:
        print(f"\n{e}\nTo register manually:\n{btcli_hint}", file=sys.stderr)
        raise

    if not quote.affordable:
        shortfall = quote.recycle_rao - quote.balance_rao
        raise RegistrationSubmissionError(
            f"coldkey {quote.coldkey_name} holds {quote.balance_rao} rao but "
            f"registration on netuid {netuid} costs {quote.recycle_rao} rao "
            f"({shortfall} rao short, before the transaction fee). "
            f"No TAO was recycled."
        )

    pre_authorized = bool(getattr(args, "register", False))
    if not pre_authorized and not sys.stdin.isatty():
        raise RegistrationSubmissionError(
            f"hotkey is not registered on netuid {netuid} and this is not an "
            f"interactive terminal, so the registration cost cannot be "
            f"confirmed. Re-run with --register to pre-authorize recycling "
            f"the live cost, or register manually:\n{btcli_hint}"
        )

    confirm_registration(quote=quote, skip=pre_authorized)

    print(
        f"submitting registration on subtensor={subtensor_network}...",
        file=sys.stderr,
    )
    try:
        uid = submit_registration(
            live_wallet=live_wallet,
            hotkey_ss58=handle.hotkey_ss58,
            netuid=netuid,
            subtensor_network=subtensor_network,
            confirmed_recycle_rao=quote.recycle_rao,
            chain_endpoint=chain_endpoint,
        )
    except RegistrationOutcomeUnknownError as e:
        # Deliberately no btcli hint: this path cannot say whether TAO was
        # already recycled, and a register command here reads as "try again".
        print(f"\n{e}", file=sys.stderr)
        raise
    except RegistrationSubmissionError as e:
        print(f"\n{e}\nTo register manually:\n{btcli_hint}", file=sys.stderr)
        raise

    print(
        f"registered on netuid {netuid}"
        + (f": uid {uid}" if uid is not None else "")
        + "\ncontinuing upload...",
        file=sys.stderr,
    )
    return uid


def _offer_owner_link(
    *,
    args: argparse.Namespace,
    agent_name: str,
    previous_wallet: AgentWalletIdentity,
    current_hotkey: str,
    network_api_url: str,
) -> None:
    """Offer a direct owner link when a known agent moves to another wallet."""
    from ditto.miner_cli.commands.attest import DEFAULT_NETUID, _run_attest

    old_coldkey = previous_wallet.coldkey_name
    old_hotkey_name = previous_wallet.hotkey_name
    old_hotkey = previous_wallet.hotkey_ss58
    command = (
        f"ditto --network {args.network} attest --coldkey {args.coldkey_name} "
        f"--hotkey {args.hotkey_name} --other-coldkey {old_coldkey} "
        f"--other-hotkey-name {old_hotkey_name}"
    )
    if not sys.stdin.isatty():
        print(
            f"notice: agent {agent_name!r} was previously uploaded with hotkey "
            f"{old_hotkey}; this upload uses {current_hotkey}. To prove both "
            f"belong to you before screening, run:\n  {command}",
            file=sys.stderr,
        )
        return
    response = (
        input(
            f"Agent {agent_name!r} previously used coldkey {old_coldkey!r} / hotkey "
            f"{old_hotkey_name!r}. This upload uses coldkey {args.coldkey_name!r} / "
            f"hotkey {args.hotkey_name!r}. Sign both hotkey endpoints now (no TAO "
            "transfer) to prove common ownership? [y/N]: "
        )
        .strip()
        .lower()
    )
    if response not in {"y", "yes"}:
        print(
            f"owner link skipped; you can create it later with:\n  {command}",
            file=sys.stderr,
        )
        return
    attest_args = argparse.Namespace(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        other_coldkey_name=old_coldkey,
        other_hotkey_name=old_hotkey_name,
        key_kind="hotkey",
        other_key_kind="hotkey",
        netuid=int(os.environ.get("NETUID", str(DEFAULT_NETUID))),
        yes=True,
        print_only=False,
    )
    try:
        _run_attest(attest_args, network_api_url=network_api_url, emit_result=False)
    except MinerCliError as exc:
        print(
            f"warning: automatic owner link failed ({exc}); upload will continue. "
            f"Retry it manually with:\n  {command}",
            file=sys.stderr,
        )


def _authorize_replacement_payment(
    *,
    args: argparse.Namespace,
    error: PaymentAmountMismatchError | PaymentRecoveryExpiredError,
    receipt: PaymentReceipt,
) -> None:
    """Require explicit consent before abandoning a finalized payment proof."""
    proof = (
        f"block={receipt.block_number} index={receipt.extrinsic_index} "
        f"hash={receipt.block_hash}"
    )
    print(
        f"warning: the platform rejected the saved payment proof ({error}).\n"
        f"Rejected proof: {proof}\n"
        "No second transfer has been sent.",
        file=sys.stderr,
    )
    if getattr(args, "pay_again", False):
        return
    if not sys.stdin.isatty():
        raise MinerCliError(
            "rerun with --pay-again to retire this rejected proof and proceed "
            "to a newly confirmed payment"
        )
    response = input(
        "Pay the currently reserved TAO fee with a new transfer? [y/N]: "
    ).strip()
    if response.lower() not in {"y", "yes"}:
        raise PaymentCancelledError(
            "new payment not authorized; the rejected proof was retained"
        )


def _retire_rejected_saved_payment(
    *,
    receipt_source: str | None,
    reassigned_payment: PendingUploadPayment | None,
    network: str,
    hotkey: str,
    name: str,
    sha256: str,
    payment: PaymentReceipt,
) -> None:
    """Remove only the locally saved proof that the platform rejected."""
    if receipt_source == "saved_reassigned":
        assert reassigned_payment is not None
        cleared = clear_pending_payment(
            network=reassigned_payment.network,
            hotkey=reassigned_payment.hotkey,
            name=reassigned_payment.name,
            sha256=reassigned_payment.sha256,
            payment=reassigned_payment.payment,
        )
    elif receipt_source == "saved":
        cleared = clear_pending_payment(
            network=network,
            hotkey=hotkey,
            name=name,
            sha256=sha256,
            payment=payment,
        )
    else:
        return
    if not cleared:
        raise MinerCliError(
            "could not retire the rejected local payment proof; no new payment "
            "was sent. Preserve the printed proof and retry."
        )


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
    admission_token: UUID,
    allow_identical_rescore: bool = False,
) -> UploadAgentResponse:
    """Retry transient post-payment failures with the same proof and archive."""
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
                    admission_token=admission_token,
                    allow_identical_rescore=allow_identical_rescore,
                )
        except SubmissionCooldownError:
            # The server supplied the actual eligibility time. Preserve the
            # finalized proof and let the miner retry then; 1/2/4-second retries
            # cannot make progress against a one-hour gate.
            raise
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
