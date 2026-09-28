"""Isolated signer-host chain adapter for a reviewed treasury payment leg.

Each leg is claimed durably before an SDK call. A crash or ambiguous result
leaves the plan in ``dispatching`` and requires independent chain inspection;
the runner will never submit it again. No module import reads a key or network.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.request import Request, urlopen

import bittensor as bt

from ditto.treasury.store import TreasuryStore

HOST_NAME = "sn118-treasury-signer"
SECRET_ID = "sn118-treasury-signing-key"
SECRET_VERSION = "1"  # Rotation requires a reviewed code/config update.
MIN_FEE_RESERVE_RAO = 1_000_000


@dataclass(frozen=True)
class FinalizedLeg:
    extrinsic_hash: str
    block_hash: str
    next_amount_rao: int | None


def _host_name() -> str:
    request = Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/name",
        headers={"Metadata-Flavor": "Google"},
    )
    with urlopen(request, timeout=3) as response:  # noqa: S310 - GCE IMDS only
        return response.read(128).decode()


def _load_wallet(project: str) -> SimpleNamespace:
    if _host_name() != HOST_NAME:
        raise RuntimeError("treasury signing is confined to its isolated host")
    result = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            SECRET_VERSION,
            f"--secret={SECRET_ID}",
            f"--project={project}",
        ],
        capture_output=True,
        check=True,
    )
    mnemonic = result.stdout.decode().strip()
    if len(mnemonic.split()) != 24:
        raise ValueError("treasury secret must be a 24-word mnemonic")
    return _memory_wallet(bt.Keypair.create_from_mnemonic(mnemonic))


def _memory_wallet(keypair: bt.Keypair) -> SimpleNamespace:
    """Expose an in-memory keypair through the Wallet surface the SDK uses.

    A disk-backed bt.Wallet would materialize the signing key on the host
    filesystem. Every SDK extrinsic this module calls first runs
    ``ExtrinsicResponse.unlock_wallet``, which invokes ``unlock_coldkey()``;
    without it each leg raises AttributeError after ``claim_leg`` and before
    anything is submitted. The keypair is already usable, so unlocking is a
    no-op.
    """
    return SimpleNamespace(
        coldkey=keypair,
        coldkeypub=keypair,
        name="sn118-treasury",
        unlock_coldkey=lambda: None,
    )


def validate_instructions(raw: bytes, plan: dict) -> None:
    """Bind current GM Billing instructions to the reviewed route and sender."""
    body = json.loads(raw)
    if not isinstance(body, dict) or set(body) != {
        "asset",
        "source",
        "destination",
        "hotkey",
        "account_ref",
    }:
        raise ValueError("exact GM payment instruction fields are required")
    intent = plan["intent"]
    expected = {
        "asset": intent["route"],
        "source": intent["linked_wallet"],
        "destination": plan["destination_coldkey"],
        "hotkey": plan["gm_hotkey"] if intent["route"] == "gm_alpha" else "",
        "account_ref": plan["gm_account_ref"],
    }
    if body != expected:
        raise ValueError("GM instructions differ from reviewed route or address")
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != intent["payment_instructions_sha256"]:
        raise ValueError("GM payment instructions digest differs from approved plan")


def _receipt(response: object, next_amount: int | None) -> FinalizedLeg:
    receipt = getattr(response, "extrinsic_receipt", None)
    if not getattr(response, "success", False) or receipt is None:
        raise RuntimeError("chain submission failed or has no receipt")
    if not getattr(receipt, "finalized", False):
        raise RuntimeError("chain submission is not finalized")
    extrinsic_hash = getattr(receipt, "extrinsic_hash", None)
    block_hash = getattr(receipt, "block_hash", None)
    if not isinstance(extrinsic_hash, str) or not re.fullmatch(
        r"0x[0-9a-f]{64}", extrinsic_hash
    ):
        raise RuntimeError("finalized receipt lacks extrinsic hash")
    if not isinstance(block_hash, str) or not re.fullmatch(
        r"0x[0-9a-f]{64}", block_hash
    ):
        raise RuntimeError("finalized receipt lacks block hash")
    return FinalizedLeg(extrinsic_hash, block_hash, next_amount)


def _balance_delta(data: object, before: str, after: str, *, increasing: bool) -> int:
    if not isinstance(data, dict):
        raise RuntimeError("SDK did not report finalized balance observations")
    a, b = data.get(before), data.get(after)
    if (
        a is None
        or b is None
        or not isinstance(a.rao, int)
        or not isinstance(b.rao, int)
    ):
        raise RuntimeError("SDK did not report integer finalized balances")
    delta = b.rao - a.rao if increasing else a.rao - b.rao
    if delta <= 0:
        raise RuntimeError("finalized balance did not move in expected direction")
    return delta


def dispatch_chain_leg(
    leg: str, amount_rao: int, plan: dict, wallet: SimpleNamespace
) -> FinalizedLeg:
    """Submit exactly one reviewed leg and return its finalized SDK receipt."""
    intent = plan["intent"]
    source = wallet.coldkeypub.ss58_address
    if source != intent["linked_wallet"]:
        raise ValueError("signer address differs from the GM-linked sender")
    if amount_rao <= 0:
        raise ValueError("positive leg amount is required")
    with bt.Subtensor(network="finney") as chain:
        free_tao_rao = chain.get_balance(source).rao
        if free_tao_rao < MIN_FEE_RESERVE_RAO:
            raise ValueError("treasury signer lacks its reserved TAO fee balance")
        if leg == "unstake":
            stake = chain.get_stake(source, plan["treasury_hotkey"], 118)
            if stake.rao < amount_rao:
                raise ValueError("finalized SN118 stake is insufficient")
            response = chain.unstake(
                wallet=wallet,
                netuid=118,
                hotkey_ss58=plan["treasury_hotkey"],
                amount=bt.Balance.from_rao(amount_rao).set_unit(118),
                safe_unstaking=True,
                allow_partial_stake=False,
                rate_tolerance=plan["max_slippage_bps"] / 10_000,
                raise_error=True,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
            proceeds = _balance_delta(
                response.data, "balance_before", "balance_after", increasing=True
            )
            if proceeds < plan["min_tao_proceeds_rao"]:
                raise RuntimeError("unstake proceeds fell below approved floor")
            return _receipt(response, min(proceeds, intent["tao_value_rao"]))
        if leg == "stake_gm":
            if free_tao_rao - amount_rao < MIN_FEE_RESERVE_RAO:
                raise ValueError("TAO balance lacks a fee reserve")
            response = chain.add_stake(
                wallet=wallet,
                netuid=28,
                hotkey_ss58=plan["gm_hotkey"],
                amount=bt.Balance.from_rao(amount_rao),
                safe_staking=True,
                allow_partial_stake=False,
                rate_tolerance=plan["max_slippage_bps"] / 10_000,
                raise_error=True,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
            received = _balance_delta(
                response.data, "stake_before", "stake_after", increasing=True
            )
            if received < plan["min_gm_alpha_rao"]:
                raise RuntimeError("GM alpha output fell below approved floor")
            return _receipt(response, received)
        if leg == "deposit_tao":
            if intent["route"] != "tao":
                raise ValueError("TAO deposit mismatches the route")
            if free_tao_rao - amount_rao < MIN_FEE_RESERVE_RAO:
                raise ValueError("TAO deposit would consume the fee reserve")
            response = chain.transfer(
                wallet=wallet,
                destination_ss58=plan["destination_coldkey"],
                amount=bt.Balance.from_rao(amount_rao),
                keep_alive=True,
                raise_error=True,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
            return _receipt(response, None)
        if leg == "deposit_gm":
            if intent["route"] != "gm_alpha":
                raise ValueError("GM alpha deposit mismatches the route")
            response = chain.transfer_stake(
                wallet=wallet,
                destination_coldkey_ss58=plan["destination_coldkey"],
                hotkey_ss58=plan["gm_hotkey"],
                origin_netuid=28,
                destination_netuid=28,
                amount=bt.Balance.from_rao(amount_rao).set_unit(28),
                raise_error=True,
                wait_for_inclusion=True,
                wait_for_finalization=True,
            )
            return _receipt(response, None)
    raise ValueError("unsupported treasury leg")


def execute_one_leg(
    store: TreasuryStore,
    key: str,
    *,
    project: str,
    instructions: bytes,
    now: datetime | None = None,
) -> FinalizedLeg:
    """Run one live leg; any error leaves a durable ambiguity and pauses."""
    if not project:
        raise ValueError("GCP project is required")
    instant = now or datetime.now(UTC)
    wallet = _load_wallet(project)
    reviewed_plan = store.plan(key)
    validate_instructions(instructions, reviewed_plan)
    if wallet.coldkeypub.ss58_address != reviewed_plan["intent"]["linked_wallet"]:
        raise ValueError("signer address differs from the reviewed GM-linked wallet")
    claimed = store.claim_leg(key, now=instant)
    try:
        result = dispatch_chain_leg(
            claimed["leg"], claimed["amount_rao"], claimed["plan"], wallet
        )
        store.finalize_leg(
            key,
            leg=claimed["leg"],
            extrinsic_hash=result.extrinsic_hash,
            block_hash=result.block_hash,
            next_amount_rao=result.next_amount_rao,
            now=datetime.now(UTC),
        )
        return result
    except BaseException:
        store.set_pause(
            True,
            operator="signer",
            reason=(
                "ambiguous or failed chain dispatch; inspect finalized chain"
                " before any recovery"
            ),
            now=datetime.now(UTC),
        )
        raise
