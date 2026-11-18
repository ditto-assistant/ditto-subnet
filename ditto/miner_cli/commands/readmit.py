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

One rule keeps a miner from paying twice or losing track of a payment: each
agent has at most one local readmission record (its own bucket, never read by
uploads), keyed by agent and never by benchmark era, and it is written before
every irreversible step:

* The whole run, from reading the record to resolving it, holds an exclusive
  per-agent OS lock, so two concurrent runs can never both see "no record"
  and both pay; the second refuses before anything. Every preferences write
  is a locked, atomic read-modify-write, so no other write can erase it.
* ``reserved`` before asking to pay; ``paying`` before the transfer is
  submitted; ``paid`` with the finalized proof once it returns. A failure to
  write ``reserved`` or ``paying`` stops before any TAO moves.
* A ``paid`` record is committed on every rerun before anything else, for the
  era it was paid for, even after the active era moved on (Platform then banks
  it as a 30-day credit rather than losing it).
* A ``paying`` record means a transfer may have been sent with no receipt
  back. Only ``--payment-*`` flags whose on-chain transfer matches it (your
  coldkey, its recorded destination and amount, finalized after it was
  recorded) or an explicit ``--discard-unconfirmed-payment`` resolve it. Any
  other input is refused and the record kept.
* ``--payment-*`` flags are written into the same record. Flags that differ
  from a saved ``paid`` proof are refused until that proof is resolved, so
  neither payment is ever dropped.
* The record is cleared only on Platform's definitive answer for its proof:
  admitted, credited, or a 32xx rejection of that exact proof.
* A record that cannot be parsed fails closed: nothing is paid, and the file
  is left for the miner to repair.
* Every message states the payment state as it actually is on disk.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from ditto.miner_cli.api_client import ApiClient
from ditto.miner_cli.commands.upload import _recovery_receipt_from_args
from ditto.miner_cli.confirm import confirm_payment
from ditto.miner_cli.errors import (
    MinerCliError,
    PaymentSubmissionError,
    ReadmissionPaymentRejectedError,
)
from ditto.miner_cli.models import PaymentReceipt
from ditto.miner_cli.network import resolve_network
from ditto.miner_cli.payment import (
    preflight_payment_signer,
    read_transfer,
    submit_eval_payment,
)
from ditto.miner_cli.preferences import (
    ReadmissionRecord,
    clear_readmission_record,
    load_readmission_record,
    preferences_path,
    readmission_lock,
    save_readmission_record,
)
from ditto.miner_cli.signing import build_readmission_payload
from ditto.miner_cli.submission_terms import show_submission_terms
from ditto.miner_cli.wallet import load_wallet, resolve_wallet_names

MEMORY_COMPETITION = "memory"


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
    recovery.add_argument(
        "--discard-unconfirmed-payment",
        action="store_true",
        dest="discard_unconfirmed_payment",
        help=(
            "Forget a transfer that was submitted but never confirmed. Only "
            "after checking your coldkey's history shows it did not go "
            "through: if it did, rerun with its --payment-* flags instead."
        ),
    )
    parser.set_defaults(func=run)
    return parser


def _recovery_flags(receipt: PaymentReceipt) -> str:
    return (
        f"--payment-block-hash {receipt.block_hash} "
        f"--payment-block-number {receipt.block_number} "
        f"--payment-extrinsic-index {receipt.extrinsic_index}"
    )


def _retry_guidance(receipt: PaymentReceipt, *, proof_saved: bool) -> str:
    """How to retry a paid readmission without sending another transfer."""
    if proof_saved:
        return (
            "Rerun the same command to retry with the saved proof, or rerun "
            f"with:\n  {_recovery_flags(receipt)}\nand no new transfer is sent."
        )
    # Nothing is saved locally: a plain rerun would reserve and pay again.
    return (
        "The proof is not saved locally. To retry without sending another "
        f"transfer, rerun with the finalized proof:\n  {_recovery_flags(receipt)}\n"
        "Do not rerun without these payment flags."
    )


def _unconfirmed_message(record: ReadmissionRecord) -> str:
    amount = f"{record.amount_rao} rao" if record.amount_rao is not None else "the fee"
    return (
        f"a readmission transfer of {amount} to {record.send_address or 'the '}"
        f"{'' if record.send_address else 'reserved address'} for agent "
        f"{record.agent_id} (v{record.bench_version}) was submitted at "
        f"{record.updated_at or 'an earlier run'} and never confirmed; it may "
        "have gone through. Nothing was sent now. Check your coldkey's transfer "
        "history: if it finalized, rerun with its --payment-block-hash, "
        "--payment-block-number and --payment-extrinsic-index; if you are sure "
        "it did not, rerun with --discard-unconfirmed-payment."
    )


# A transfer's block may carry a timestamp slightly before the local clock
# that recorded ``paying``; anything older is a different, earlier transfer.
_CLOCK_SKEW = timedelta(minutes=10)
_TRANSFER_CALL = "Balances.transfer_keep_alive"


def _describe_record(record: ReadmissionRecord) -> str:
    """The payment state a record proves, in words that never overstate it."""
    if record.state == "paid" and record.payment is not None:
        return (
            f"a finalized readmission payment is saved for v{record.bench_version} "
            f"({_recovery_flags(record.payment)})"
        )
    if record.state == "paying":
        return (
            f"an unconfirmed readmission transfer of {record.amount_rao} rao to "
            f"{record.send_address} for v{record.bench_version} is saved; it may "
            "have gone through"
        )
    return f"a readmission price reservation for v{record.bench_version} is saved"


def _verify_unconfirmed_transfer(
    record: ReadmissionRecord,
    proof: PaymentReceipt,
    *,
    coldkey_ss58: str,
    subtensor_network: str,
    chain_endpoint: str | None,
) -> None:
    """Refuse recovery flags that are not this ``paying`` record's transfer.

    The record keeps the amount, destination and time of the transfer it
    guards; the flags are accepted only if the extrinsic they point at is a
    transfer from this coldkey matching all three. Otherwise the record is
    kept, so an unrelated (say, an earlier upload-fee) proof can never stand
    in for it and then be cleared on rejection.
    """
    kept = "The unconfirmed transfer record is kept and nothing was committed or sent."
    try:
        transfer = read_transfer(
            receipt=proof,
            subtensor_network=subtensor_network,
            chain_endpoint=chain_endpoint,
        )
    except PaymentSubmissionError as error:
        raise MinerCliError(
            f"could not check those --payment-* flags on chain ({error}). {kept} "
            "Retry when the chain is reachable"
        ) from error
    problems: list[str] = []
    if transfer.call != _TRANSFER_CALL:
        problems.append(f"it is a {transfer.call} call, not {_TRANSFER_CALL}")
    if transfer.signer != coldkey_ss58:
        problems.append(f"it was signed by {transfer.signer}, not your coldkey")
    if transfer.dest != record.send_address:
        problems.append(f"it paid {transfer.dest}, not {record.send_address}")
    if transfer.amount_rao != record.amount_rao:
        problems.append(f"it moved {transfer.amount_rao} rao, not {record.amount_rao}")
    started = _parse_time(record.updated_at)
    if started is not None:
        finalized = (
            datetime.fromtimestamp(transfer.timestamp_ms / 1000, tz=started.tzinfo)
            if transfer.timestamp_ms is not None
            else None
        )
        if finalized is None or finalized < started - _CLOCK_SKEW:
            problems.append(
                f"it finalized at {finalized or 'an unknown time'}, before this "
                f"transfer was submitted ({record.updated_at})"
            )
    if problems:
        raise MinerCliError(
            "those --payment-* flags are not the unconfirmed readmission "
            f"transfer: {'; '.join(problems)}. {kept} Pass the flags of the "
            f"transfer of {record.amount_rao} rao to {record.send_address} made "
            f"at or after {record.updated_at or 'the recorded time'}, or "
            "--discard-unconfirmed-payment if your coldkey history shows it "
            "never went through"
        )


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _active_bench_version(terms: dict[str, Any] | None) -> int:
    readmission = (terms or {}).get("readmission")
    activation = (
        readmission.get("activation") if isinstance(readmission, dict) else None
    )
    if not isinstance(activation, dict):
        activation = None
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
    discard = bool(getattr(args, "discard_unconfirmed_payment", False))
    if discard and recovery is not None:
        raise MinerCliError(
            "--discard-unconfirmed-payment and --payment-* contradict each other: "
            "pass the flags if the transfer went through, or discard it if it "
            "did not (nothing was sent)"
        )
    coldkey_name, hotkey_name = resolve_wallet_names(
        coldkey_name=args.coldkey_name,
        hotkey_name=args.hotkey_name,
        interactive=sys.stdin.isatty() and not args.yes,
    )
    handle, wallet = load_wallet(coldkey_name=coldkey_name, hotkey_name=hotkey_name)
    hotkey = handle.hotkey_ss58
    network = resolve_network(args.network)
    net = network.name
    with ApiClient(base_url=network.api_url) as client:
        terms = show_submission_terms(client, MEMORY_COMPETITION)
        # Held from the first read of the record until it is resolved.
        with readmission_lock(network=net, hotkey=hotkey, agent_id=str(agent_id)):
            return _readmit_locked(
                args=args,
                client=client,
                wallet=wallet,
                terms=terms,
                net=net,
                hotkey=hotkey,
                coldkey_name=coldkey_name,
                agent_id=agent_id,
                recovery=recovery,
                discard=discard,
                subtensor_network=network.subtensor_network,
            )


def _readmit_locked(
    *,
    args: argparse.Namespace,
    client: ApiClient,
    wallet: Any,
    terms: dict[str, Any] | None,
    net: str,
    hotkey: str,
    coldkey_name: str,
    agent_id: UUID,
    recovery: PaymentReceipt | None,
    discard: bool,
    subtensor_network: str,
) -> int:
    ledger = {"network": net, "hotkey": hotkey, "agent_id": str(agent_id)}
    record = load_readmission_record(**ledger)  # malformed raises: fail closed
    if record is not None and record.state == "reserved":
        # Nothing was paid under a bare reservation: start over.
        record = None

    # An unconfirmed transfer is resolved before anything else: by its own
    # proof (verified below), or by the miner's explicit word that it did not
    # go through.
    if record is not None and record.state == "paying" and recovery is None:
        if not discard:
            raise MinerCliError(_unconfirmed_message(record))
        if not clear_readmission_record(
            network=net, hotkey=hotkey, agent_id=str(agent_id)
        ):
            raise MinerCliError(
                f"could not remove the unconfirmed transfer record from "
                f"{preferences_path()}; it is kept and nothing was sent"
            )
        print("discarded the unconfirmed readmission transfer record", file=sys.stderr)
        record = None

    # The era a payment was made for wins over the active one.
    if record is not None:
        bench_version = record.bench_version
        if args.bench_version is not None and args.bench_version != bench_version:
            raise MinerCliError(
                f"agent {agent_id}: {_describe_record(record)}. It is resolved "
                f"for v{bench_version} first, not v{args.bench_version}: rerun "
                "without --bench-version. Nothing was sent now and the saved "
                "record is kept"
            )
    else:
        bench_version = (
            args.bench_version
            if args.bench_version is not None
            else _active_bench_version(terms)
        )

    own_transfer = False
    if recovery is not None:
        if record is not None and record.state == "paid" and record.payment != recovery:
            raise MinerCliError(
                f"agent {agent_id}: {_describe_record(record)}, and it differs "
                "from these --payment-* flags. Resolve it first by rerunning "
                "without payment flags. Nothing was sent now and the saved "
                "proof is kept"
            )
        if record is not None and record.state == "paid":
            proof_saved = True
        else:
            was_paying = record is not None and record.state == "paying"
            if record is not None and was_paying:
                _verify_unconfirmed_transfer(
                    record,
                    recovery,
                    coldkey_ss58=str(wallet.coldkeypub.ss58_address),
                    subtensor_network=subtensor_network,
                    chain_endpoint=getattr(args, "chain_endpoint", None) or None,
                )
            record = ReadmissionRecord(
                agent_id=str(agent_id),
                bench_version=bench_version,
                state="paid",
                token=record.token if record is not None else None,
                amount_rao=record.amount_rao if record is not None else None,
                send_address=record.send_address if record is not None else None,
                payment=recovery,
            )
            proof_saved = save_readmission_record(
                network=net, hotkey=hotkey, record=record
            )
            if was_paying and not proof_saved:
                raise MinerCliError(
                    "the proof matches the unconfirmed transfer, but it could not "
                    f"be saved to {preferences_path()}. The unconfirmed record is "
                    "kept and nothing was committed; rerun with the same flags "
                    "once the config is writable"
                )
        receipt = recovery
    elif record is not None and record.payment is not None:
        receipt = record.payment
        proof_saved = True
        print(
            "found a saved finalized readmission payment for this agent "
            f"(v{bench_version}); no new transfer will be sent",
            file=sys.stderr,
        )
    else:
        record, receipt, proof_saved = _reserve_and_pay(
            args=args,
            client=client,
            wallet=wallet,
            net=net,
            hotkey=hotkey,
            coldkey_name=coldkey_name,
            agent_id=agent_id,
            bench_version=bench_version,
            subtensor_network=subtensor_network,
        )
        own_transfer = True

    # This run's own transfer may be on disk as ``paid`` or, if that save
    # failed, still ``paying``: either way the record is this proof's (the
    # lock is held), so it is resolved whole. A supplied proof only ever
    # resolves a record holding exactly that proof.
    resolve_payment = None if own_transfer else receipt

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
    # Without a token Platform falls back to the live reservation it holds
    # for this agent, era and hotkey, so the price snapshot survives a
    # lost local config.
    if record.token is not None:
        body["admission_token"] = record.token
    try:
        result = client.post_readmission_commit(body)
    except ReadmissionPaymentRejectedError:
        # Definitive for this exact proof, however it was supplied; a
        # different saved proof is never touched.
        if clear_readmission_record(
            network=net, hotkey=hotkey, agent_id=str(agent_id), payment=resolve_payment
        ):
            print(
                "\nplatform refused the payment proof; its local record was "
                "removed. Rerunning reads the current price and asks before "
                "paying again.",
                file=sys.stderr,
            )
        else:
            print(
                "\nplatform refused the payment proof, but the saved readmission "
                f"record in {preferences_path()} was not removed: "
                f"{_describe_remaining(ledger)}. A rerun resolves that record "
                "first.",
                file=sys.stderr,
            )
        raise
    except BaseException:
        print(
            "\nthe readmission could not be confirmed after payment. The fee "
            "is not spent until Platform records it. "
            + _retry_guidance(receipt, proof_saved=proof_saved),
            file=sys.stderr,
        )
        raise
    outcome = result.get("outcome") if isinstance(result, dict) else None
    if outcome not in ("admitted", "credited"):
        # The record stays, so a rerun retries with it.
        raise MinerCliError(
            "platform returned an invalid readmission response "
            f"(outcome {outcome!r}). "
            + _retry_guidance(receipt, proof_saved=proof_saved)
        )
    if not clear_readmission_record(
        network=net, hotkey=hotkey, agent_id=str(agent_id), payment=resolve_payment
    ):
        print(
            f"note: Platform recorded this payment, but the saved record in "
            f"{preferences_path()} was not removed: {_describe_remaining(ledger)}. "
            "A rerun commits it again and Platform refuses the spent proof, "
            "which then removes it; no new transfer is sent.",
            file=sys.stderr,
        )
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


def _describe_remaining(ledger: dict[str, str]) -> str:
    try:
        remaining = load_readmission_record(**ledger)
    except MinerCliError as error:
        return str(error)
    if remaining is None:
        return "no record is saved"
    return _describe_record(remaining)


def _reserve_and_pay(
    *,
    args: argparse.Namespace,
    client: ApiClient,
    wallet: Any,
    net: str,
    hotkey: str,
    coldkey_name: str,
    agent_id: UUID,
    bench_version: int,
    subtensor_network: str,
) -> tuple[ReadmissionRecord, PaymentReceipt, bool]:
    """Check, reserve and pay, writing the record before each irreversible step.

    Returns the ``paid`` record, the proof, and whether that record persisted.
    """
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
    record = ReadmissionRecord(
        agent_id=str(agent_id),
        bench_version=bench_version,
        state="reserved",
        token=token,
        amount_rao=amount_rao,
        send_address=send_address,
    )
    if not save_readmission_record(network=net, hotkey=hotkey, record=record):
        raise MinerCliError(
            "could not save the readmission reservation locally; no payment was "
            "sent. Check that the CLI config is writable and rerun"
        )
    chain_endpoint = getattr(args, "chain_endpoint", None) or None
    preflight_payment_signer(
        live_wallet=wallet,
        hotkey_ss58=hotkey,
        subtensor_network=subtensor_network,
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
    # From here a transfer may happen. Record that first, so an outcome we
    # never learn (a timeout, a crash) blocks any second transfer.
    record = replace(record, state="paying")
    if not save_readmission_record(network=net, hotkey=hotkey, record=record):
        raise MinerCliError(
            "could not record the pending transfer locally; no payment was "
            "sent. Check that the CLI config is writable and rerun"
        )
    print(f"submitting payment on subtensor={subtensor_network}...", file=sys.stderr)
    try:
        receipt = submit_eval_payment(
            live_wallet=wallet,
            subtensor_network=subtensor_network,
            amount_rao=amount_rao,
            dest_address=send_address,
            chain_endpoint=chain_endpoint,
        )
    except BaseException:
        print(
            "\nthe transfer's outcome is unknown, and it may still finalize. "
            "Do not pay again: a rerun refuses to until you either supply its "
            "--payment-* flags (from your coldkey's transfer history) or run "
            "--discard-unconfirmed-payment after confirming it never went through.",
            file=sys.stderr,
        )
        raise
    print(
        f"payment finalised: block_hash={receipt.block_hash} "
        f"block={receipt.block_number} ext_idx={receipt.extrinsic_index}",
        file=sys.stderr,
    )
    record = replace(record, state="paid", payment=receipt)
    saved = save_readmission_record(network=net, hotkey=hotkey, record=record)
    if not saved:
        print(
            "warning: could not save the finalized payment proof locally; keep "
            "the printed proof if the readmission fails",
            file=sys.stderr,
        )
    return record, receipt, saved


__all__ = ["add_subparser", "run"]
