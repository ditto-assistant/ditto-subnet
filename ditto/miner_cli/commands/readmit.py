"""``ditto readmit AGENT_ID``: pay to readmit your own agent into the active era.

SN-485. When a benchmark era changes, an agent you already uploaded that
finished scoring on the previous era is not carried into the new one unless it
was in the frozen rescore cohort. ``ditto readmit`` re-enters exactly that
agent, unchanged, for the readmission fee, which is the upload fee. It counts
toward, and starts, your coldkey's upload cooldown.

The flow mirrors ``ditto upload``:

1. ``POST /readmission/check`` reports every reason Platform would refuse
   (inactive, not your agent, not stranded, partial, withdrawn, retired,
   below the policy floor, open rollout, cooldown) **before anything is paid**,
   and reserves the price for 24 hours.
2. One ``transfer_keep_alive`` of exactly the reserved amount to the reserved
   address, confirmed by you first.
3. ``POST /readmission/commit`` with the finalized proof. ``admitted`` means
   the agent will be leased on the active era. ``credited`` means Platform
   refused it after payment (the era moved, the agent was withdrawn) and kept
   your payment as an upload credit for 30 days. Ditto never refunds on chain.

The finalized proof is saved in its own local bucket before the commit, so a
lost response or a crash is recovered by rerunning the same command. Uploads
never read that bucket.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from typing import Any
from uuid import UUID, uuid4

from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands.upload import _recovery_receipt_from_args
from ditto.miner_cli.confirm import confirm_payment
from ditto.miner_cli.errors import MinerCliError, ReadmissionPaymentRejectedError
from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.payment import preflight_payment_signer, submit_eval_payment
from ditto.miner_cli.preferences import (
    READMISSION_PAYMENTS_BUCKET,
    clear_pending_payment,
    clear_readmission_token,
    load_pending_payment,
    load_readmission_token,
    save_pending_payment,
    save_readmission_token,
)
from ditto.miner_cli.signing import build_readmission_payload
from ditto.miner_cli.submission_terms import show_submission_terms
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

MEMORY_COMPETITION = "memory"
PENDING_PAYMENT_NAME = "readmission"


def add_subparser(
    subparsers: argparse._SubParsersAction,
    *,
    parents: list[argparse.ArgumentParser] | None = None,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "readmit",
        help="Pay the upload fee to readmit your own agent into the active era.",
        description=(
            "Readmit an agent you already uploaded, unchanged, into the active "
            "benchmark era (SN-485). Platform checks eligibility before you pay; "
            "the fee equals the upload fee and starts your upload cooldown."
        ),
        parents=parents or [],
    )
    parser.add_argument("agent_id", type=UUID, help="The agent to readmit.")
    parser.add_argument(
        "--bench-version",
        type=int,
        dest="bench_version",
        help="Benchmark era to readmit into (default: the active one).",
    )
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
    recovery = parser.add_argument_group(
        "payment recovery",
        "Reuse a finalized readmission payment after the commit failed or its "
        "response was lost. All three are required together; no new transfer "
        "is sent.",
    )
    recovery.add_argument("--payment-block-hash")
    recovery.add_argument("--payment-block-number", type=int)
    recovery.add_argument("--payment-extrinsic-index", type=int)
    parser.set_defaults(func=run)
    return parser


def _pending_key(agent_id: UUID, bench_version: int) -> str:
    """A 64-hex local key for one (agent, era) readmission proof."""
    return hashlib.sha256(f"{agent_id}:{bench_version}".encode()).hexdigest()


def _recovery_flags(receipt: PaymentReceipt) -> str:
    return (
        f"--payment-block-hash {receipt.block_hash} "
        f"--payment-block-number {receipt.block_number} "
        f"--payment-extrinsic-index {receipt.extrinsic_index}"
    )


def _active_bench_version(terms: dict[str, Any] | None) -> int:
    readmission = (terms or {}).get("readmission")
    activation = (
        readmission.get("activation") if isinstance(readmission, dict) else None
    )
    version = activation.get("active_bench_version") if activation else None
    if not isinstance(version, int) or isinstance(version, bool):
        raise MinerCliError(
            "this Platform does not offer readmission yet; no payment was sent"
        )
    return version


def _signed_body(
    *,
    wallet: Any,
    operation: str,
    hotkey: str,
    agent_id: UUID,
    bench_version: int,
) -> dict[str, Any]:
    nonce = uuid4()
    timestamp = int(time.time())
    signature = wallet.hotkey.sign(
        build_readmission_payload(
            operation=operation,
            hotkey_ss58=hotkey,
            agent_id=agent_id,
            bench_version=bench_version,
            signature_timestamp=timestamp,
            signature_nonce=nonce,
        )
    ).hex()
    return {
        "hotkey": hotkey,
        "agent_id": str(agent_id),
        "bench_version": bench_version,
        "signature": signature,
        "signature_timestamp": timestamp,
        "signature_nonce": str(nonce),
    }


def _describe_eligibility(check: dict[str, Any]) -> None:
    eligibility = check.get("eligibility") or {}
    if eligibility.get("owner_best_stranded") is False:
        print(
            "note: this is not your best stranded agent. Payouts count only your "
            "best agent, so readmitting this one may not change what you earn.",
            file=sys.stderr,
        )
    sibling = eligibility.get("cohort_sibling_agent_id")
    if sibling:
        print(
            f"note: you already have agent {sibling} on the active era through "
            "the rescore cohort.",
            file=sys.stderr,
        )


def run(args: argparse.Namespace) -> int:
    agent_id: UUID = args.agent_id
    recovery = _recovery_receipt_from_args(args)
    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not args.yes,
    )
    handle, wallet = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
    hotkey = handle.hotkey_ss58
    network = resolve_network(args.network)
    with ApiClient(base_url=network.api_url) as client:
        terms = show_submission_terms(client, MEMORY_COMPETITION)
        bench_version = (
            args.bench_version
            if args.bench_version is not None
            else _active_bench_version(terms)
        )
        pending = {
            "network": network.name,
            "hotkey": hotkey,
            "name": PENDING_PAYMENT_NAME,
            "sha256": _pending_key(agent_id, bench_version),
            "bucket": READMISSION_PAYMENTS_BUCKET,
        }
        token_key = {
            "network": network.name,
            "hotkey": hotkey,
            "key": _pending_key(agent_id, bench_version),
        }
        receipt = recovery
        # A saved reservation token restores the price snapshot on both the
        # saved-proof and the explicit-proof recovery paths.
        token: str | None = load_readmission_token(**token_key)
        if receipt is None:
            receipt = load_pending_payment(**pending)
            if receipt is not None:
                print(
                    "found a saved finalized readmission payment for this agent "
                    "and era; no new transfer will be sent",
                    file=sys.stderr,
                )
        if receipt is None:
            check = client.post_readmission_check(
                {
                    **_signed_body(
                        wallet=wallet,
                        operation="reserve",
                        hotkey=hotkey,
                        agent_id=agent_id,
                        bench_version=bench_version,
                    ),
                    "reserve_slot": True,
                }
            )
            if not check.get("ok"):
                messages = check.get("messages") or ["refused"]
                raise MinerCliError(
                    "platform would refuse this readmission (no payment was sent): "
                    + "; ".join(str(m) for m in messages)
                )
            _describe_eligibility(check)
            amount_rao = check.get("payment_amount_rao")
            send_address = check.get("payment_send_address")
            token = check.get("admission_token")
            if (
                not isinstance(amount_rao, int)
                or isinstance(amount_rao, bool)
                or amount_rao <= 0
                or not isinstance(send_address, str)
                or not isinstance(token, str)
            ):
                raise MinerCliError(
                    "platform did not reserve a readmission price; no payment was sent"
                )
            chain_endpoint = getattr(args, "chain_endpoint", None) or None
            preflight_payment_signer(
                live_wallet=wallet,
                hotkey_ss58=hotkey,
                subtensor_network=network.subtensor_network,
                chain_endpoint=chain_endpoint,
            )
            confirm_payment(
                amount_rao=amount_rao,
                dest_address=send_address,
                hotkey_ss58=hotkey,
                coldkey_name=coldkey_name,
                skip=args.yes,
                purpose=f"readmission of agent {agent_id} into v{bench_version} "
                "(the upload fee)",
            )
            print(
                f"submitting payment on subtensor={network.subtensor_network}...",
                file=sys.stderr,
            )
            receipt = submit_eval_payment(
                live_wallet=wallet,
                subtensor_network=network.subtensor_network,
                amount_rao=amount_rao,
                dest_address=send_address,
                chain_endpoint=chain_endpoint,
            )
            print(
                f"payment finalised: block_hash={receipt.block_hash} "
                f"block={receipt.block_number} ext_idx={receipt.extrinsic_index}",
                file=sys.stderr,
            )
            if not save_pending_payment(**pending, payment=receipt):
                print(
                    "warning: could not save the finalized payment proof locally; "
                    "keep the printed proof if the readmission fails",
                    file=sys.stderr,
                )
            # Kept with the proof: a rerun commits against the same price
            # snapshot the payment was made for.
            save_readmission_token(**token_key, token=token)
        body = {
            **_signed_body(
                wallet=wallet,
                operation="commit",
                hotkey=hotkey,
                agent_id=agent_id,
                bench_version=bench_version,
            ),
            "payment_block_hash": receipt.block_hash,
            "payment_block_number": receipt.block_number,
            "payment_extrinsic_index": receipt.extrinsic_index,
        }
        if token is not None:
            body["admission_token"] = token
        try:
            result = client.post_readmission_commit(body)
            outcome = result["outcome"]
        except ReadmissionPaymentRejectedError:
            if recovery is None:
                clear_pending_payment(**pending, payment=receipt)
                clear_readmission_token(**token_key)
                print(
                    "\nplatform refused the payment proof; it was removed from "
                    "local storage. Rerunning reads the current price and asks "
                    "before paying again.",
                    file=sys.stderr,
                )
            raise
        except BaseException:
            print(
                "\nthe readmission could not be confirmed after payment. The fee "
                "is not spent until Platform records it. Rerun the same command "
                "to retry with the saved proof, or rerun with:\n"
                f"  {_recovery_flags(receipt)}\n"
                "and no new transfer is sent.",
                file=sys.stderr,
            )
            raise
    clear_pending_payment(**pending, payment=receipt)
    clear_readmission_token(**token_key)
    if outcome == "admitted":
        print(str(agent_id))
        print(
            f"readmitted agent {agent_id} into v{result.get('bench_version')}; "
            "validators will lease it on the active era",
            file=sys.stderr,
        )
        return 0
    print(
        f"platform refused the readmission after payment: "
        f"{result.get('refusal_reason')}\n"
        "Your payment is kept as an upload credit for this hotkey until "
        f"{result.get('credit_expires_at')}. Spend it on your next upload with:\n"
        f"  ditto upload ... {_recovery_flags(receipt)}\n"
        "No TAO is refunded on chain.",
        file=sys.stderr,
    )
    return 1


__all__ = ["add_subparser", "run"]
