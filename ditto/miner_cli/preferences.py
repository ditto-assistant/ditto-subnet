"""Small, human-editable miner CLI preferences."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ditto.miner_cli.models import PaymentReceipt, PendingUploadPayment


@dataclass(frozen=True)
class AgentWalletIdentity:
    """One locally remembered wallet that successfully uploaded an agent."""

    coldkey_name: str
    hotkey_name: str
    hotkey_ss58: str


def preferences_path() -> Path:
    """Return the preferences file, honoring test/operator overrides."""
    override = os.environ.get("DITTO_CLI_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "ditto" / "config.json"


def _key(*, network: str, hotkey: str) -> str:
    return f"{network}:{hotkey}"


# Agent upload proofs. Agent uploads scan this whole bucket for reusable
# credits, so a proof for another lane must live in its own bucket.
AGENT_PAYMENTS_BUCKET = "pending_upload_payments"
GENERATOR_PAYMENTS_BUCKET = "pending_generator_payments"
# ``ditto readmit`` ledger (SN-485): one record per agent for its unresolved
# readmission payment. Never scanned by uploads: a readmission proof must not
# be reused as an upload's reusable credit (Platform refuses it there, 3207).
READMISSION_LEDGER_BUCKET = "pending_readmissions"


def _pending_payment_key(*, network: str, hotkey: str, name: str, sha256: str) -> str:
    identity = f"{network}\0{hotkey}\0{name}\0{sha256}".encode()
    return hashlib.sha256(identity).hexdigest()


def _load_preferences() -> dict:
    try:
        raw = json.loads(preferences_path().read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_preferences(raw: dict) -> bool:
    path = preferences_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
        return True
    except OSError:
        return False


def load_agent_name(*, network: str, hotkey: str) -> str | None:
    """Load the last successful agent name for one network and hotkey."""
    raw = _load_preferences()
    name = raw.get("agent_names", {}).get(_key(network=network, hotkey=hotkey))
    return name if isinstance(name, str) and 1 <= len(name) <= 64 else None


def save_agent_name(*, network: str, hotkey: str, name: str) -> bool:
    """Atomically remember a successful upload name; return whether it persisted."""
    raw = _load_preferences()
    names = raw.get("agent_names")
    if not isinstance(names, dict):
        names = {}
    names[_key(network=network, hotkey=hotkey)] = name
    raw["agent_names"] = names
    return _save_preferences(raw)


def load_previous_agent_wallet(
    *, network: str, name: str, excluding_hotkey: str
) -> AgentWalletIdentity | None:
    """Return the most recently saved different wallet for an agent name."""
    raw = _load_preferences()
    histories = raw.get("agent_wallet_history")
    if not isinstance(histories, dict):
        return None
    values = histories.get(f"{network}:{name}")
    if not isinstance(values, list):
        return None
    for value in reversed(values):
        if not isinstance(value, dict) or value.get("hotkey_ss58") == excluding_hotkey:
            continue
        coldkey_name = value.get("coldkey_name")
        hotkey_name = value.get("hotkey_name")
        hotkey_ss58 = value.get("hotkey_ss58")
        if (
            isinstance(coldkey_name, str)
            and bool(coldkey_name)
            and isinstance(hotkey_name, str)
            and bool(hotkey_name)
            and isinstance(hotkey_ss58, str)
            and bool(hotkey_ss58)
        ):
            return AgentWalletIdentity(
                coldkey_name=coldkey_name,
                hotkey_name=hotkey_name,
                hotkey_ss58=hotkey_ss58,
            )
    return None


def save_agent_wallet(
    *,
    network: str,
    name: str,
    coldkey_name: str,
    hotkey_name: str,
    hotkey_ss58: str,
) -> bool:
    """Remember a successful upload wallet without storing any key material."""
    raw = _load_preferences()
    histories = raw.get("agent_wallet_history")
    if not isinstance(histories, dict):
        histories = {}
    key = f"{network}:{name}"
    values = histories.get(key)
    if not isinstance(values, list):
        values = []
    values = [
        value
        for value in values
        if not isinstance(value, dict) or value.get("hotkey_ss58") != hotkey_ss58
    ]
    values.append(
        {
            "coldkey_name": coldkey_name,
            "hotkey_name": hotkey_name,
            "hotkey_ss58": hotkey_ss58,
        }
    )
    histories[key] = values[-10:]
    raw["agent_wallet_history"] = histories
    return _save_preferences(raw)


def load_pending_payment(
    *,
    network: str,
    hotkey: str,
    name: str,
    sha256: str,
    bucket: str = AGENT_PAYMENTS_BUCKET,
) -> PaymentReceipt | None:
    """Load a finalized proof saved for this exact local upload identity."""
    raw = _load_preferences()
    payments = raw.get(bucket)
    if not isinstance(payments, dict):
        return None
    value = payments.get(
        _pending_payment_key(network=network, hotkey=hotkey, name=name, sha256=sha256)
    )
    if not isinstance(value, dict):
        return None
    block_hash = value.get("block_hash")
    block_number = value.get("block_number")
    extrinsic_index = value.get("extrinsic_index")
    if (
        not isinstance(block_hash, str)
        or not block_hash.startswith("0x")
        or len(block_hash) != 66
        or not isinstance(block_number, int)
        or block_number < 1
        or not isinstance(extrinsic_index, int)
        or extrinsic_index < 0
    ):
        return None
    return PaymentReceipt(
        block_hash=block_hash,
        block_number=block_number,
        extrinsic_index=extrinsic_index,
    )


def load_pending_payments_for_hotkey(
    *, network: str, hotkey: str
) -> tuple[PendingUploadPayment, ...]:
    """Discover reusable credits saved by compatible clients for one hotkey."""
    raw = _load_preferences()
    payments = raw.get(AGENT_PAYMENTS_BUCKET)
    if not isinstance(payments, dict):
        return ()
    found: list[PendingUploadPayment] = []
    for value in payments.values():
        if not isinstance(value, dict):
            continue
        if value.get("network") != network or value.get("hotkey") != hotkey:
            continue
        name = value.get("name")
        sha256 = value.get("sha256")
        block_hash = value.get("block_hash")
        block_number = value.get("block_number")
        extrinsic_index = value.get("extrinsic_index")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 64
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or not isinstance(block_hash, str)
            or not block_hash.startswith("0x")
            or len(block_hash) != 66
            or not isinstance(block_number, int)
            or block_number < 1
            or not isinstance(extrinsic_index, int)
            or extrinsic_index < 0
        ):
            continue
        found.append(
            PendingUploadPayment(
                network=network,
                hotkey=hotkey,
                name=name,
                sha256=sha256,
                payment=PaymentReceipt(
                    block_hash=block_hash,
                    block_number=block_number,
                    extrinsic_index=extrinsic_index,
                ),
            )
        )
    return tuple(found)


def save_pending_payment(
    *,
    network: str,
    hotkey: str,
    name: str,
    sha256: str,
    payment: PaymentReceipt,
    bucket: str = AGENT_PAYMENTS_BUCKET,
) -> bool:
    """Persist a finalized proof before attempting the corresponding upload."""
    raw = _load_preferences()
    payments = raw.get(bucket)
    if not isinstance(payments, dict):
        payments = {}
    payments[
        _pending_payment_key(network=network, hotkey=hotkey, name=name, sha256=sha256)
    ] = {
        "network": network,
        "hotkey": hotkey,
        "name": name,
        "sha256": sha256,
        "block_hash": payment.block_hash,
        "block_number": payment.block_number,
        "extrinsic_index": payment.extrinsic_index,
    }
    raw[bucket] = payments
    return _save_preferences(raw)


def load_miner_session(*, network: str) -> dict | None:
    raw = _load_preferences()
    sessions = raw.get("miner_sessions")
    if not isinstance(sessions, dict):
        return None
    value = sessions.get(network)
    if not isinstance(value, dict):
        return None
    token = value.get("token")
    hotkey = value.get("hotkey")
    if not isinstance(token, str) or not token.startswith("ditto_ms_"):
        return None
    if not isinstance(hotkey, str) or not hotkey:
        return None
    return value


def save_miner_session(
    *,
    network: str,
    token: str,
    hotkey: str,
    scopes: list[str],
    expires_at: str,
) -> bool:
    raw = _load_preferences()
    sessions = raw.get("miner_sessions")
    if not isinstance(sessions, dict):
        sessions = {}
    sessions[network] = {
        "token": token,
        "hotkey": hotkey,
        "scopes": scopes,
        "expires_at": expires_at,
    }
    raw["miner_sessions"] = sessions
    return _save_preferences(raw)


def clear_miner_session(*, network: str) -> bool:
    raw = _load_preferences()
    sessions = raw.get("miner_sessions")
    if not isinstance(sessions, dict) or network not in sessions:
        return True
    del sessions[network]
    raw["miner_sessions"] = sessions
    return _save_preferences(raw)


def clear_pending_payment(
    *,
    network: str,
    hotkey: str,
    name: str,
    sha256: str,
    payment: PaymentReceipt,
    bucket: str = AGENT_PAYMENTS_BUCKET,
) -> bool:
    """Clear only the matching proof after a confirmed upload response."""
    raw = _load_preferences()
    payments = raw.get(bucket)
    if not isinstance(payments, dict):
        return True
    key = _pending_payment_key(network=network, hotkey=hotkey, name=name, sha256=sha256)
    current = payments.get(key)
    expected = {
        "block_hash": payment.block_hash,
        "block_number": payment.block_number,
        "extrinsic_index": payment.extrinsic_index,
    }
    if not isinstance(current, dict) or any(
        current.get(field) != value for field, value in expected.items()
    ):
        return True
    del payments[key]
    raw[bucket] = payments
    return _save_preferences(raw)


ReadmissionState = Literal["reserved", "paying", "paid"]


@dataclass(frozen=True)
class ReadmissionRecord:
    """The one unresolved readmission payment of one agent (SN-485).

    Keyed by network, hotkey and agent, never by benchmark era, so a rerun
    after the active era changes still finds it. ``bench_version`` is the era
    the reservation and payment were made for. The states, each written
    before the step it guards:

    * ``reserved``: a price reservation exists; nothing was paid.
    * ``paying``: a transfer is about to be, or was, submitted and no receipt
      came back. It may have finalized, so nothing is paid again until the
      miner supplies its proof or explicitly discards it.
    * ``paid``: ``payment`` is the finalized proof; reruns commit it.
    """

    agent_id: str
    bench_version: int
    state: ReadmissionState
    token: str | None = None
    amount_rao: int | None = None
    send_address: str | None = None
    payment: PaymentReceipt | None = None
    updated_at: str = ""


def _ledger_key(network: str, hotkey: str, agent_id: str) -> str:
    return f"{network}:{hotkey}:{agent_id}"


def _parse_record(value: object) -> ReadmissionRecord | None:
    if not isinstance(value, dict):
        return None
    agent_id = value.get("agent_id")
    bench_version = value.get("bench_version")
    state = value.get("state")
    if (
        not isinstance(agent_id, str)
        or not isinstance(bench_version, int)
        or isinstance(bench_version, bool)
        or state not in ("reserved", "paying", "paid")
    ):
        return None
    payment = None
    block_hash = value.get("block_hash")
    block_number = value.get("block_number")
    extrinsic_index = value.get("extrinsic_index")
    if (
        isinstance(block_hash, str)
        and block_hash.startswith("0x")
        and len(block_hash) == 66
        and isinstance(block_number, int)
        and block_number >= 1
        and isinstance(extrinsic_index, int)
        and extrinsic_index >= 0
    ):
        payment = PaymentReceipt(
            block_hash=block_hash,
            block_number=block_number,
            extrinsic_index=extrinsic_index,
        )
    if state == "paid" and payment is None:
        return None
    token = value.get("token")
    amount = value.get("amount_rao")
    address = value.get("send_address")
    updated = value.get("updated_at")
    return ReadmissionRecord(
        agent_id=agent_id,
        bench_version=bench_version,
        state=state,
        token=token if isinstance(token, str) and token else None,
        amount_rao=amount
        if isinstance(amount, int) and not isinstance(amount, bool)
        else None,
        send_address=address if isinstance(address, str) else None,
        payment=payment,
        updated_at=updated if isinstance(updated, str) else "",
    )


def load_readmission_record(
    *, network: str, hotkey: str, agent_id: str
) -> ReadmissionRecord | None:
    """This agent's unresolved readmission payment, whatever era it was for."""
    raw = _load_preferences()
    ledger = raw.get(READMISSION_LEDGER_BUCKET)
    if not isinstance(ledger, dict):
        return None
    return _parse_record(ledger.get(_ledger_key(network, hotkey, agent_id)))


def save_readmission_record(
    *, network: str, hotkey: str, record: ReadmissionRecord
) -> bool:
    """Atomically replace this agent's record; returns whether it persisted."""
    raw = _load_preferences()
    ledger = raw.get(READMISSION_LEDGER_BUCKET)
    if not isinstance(ledger, dict):
        ledger = {}
    value: dict[str, object] = {
        "agent_id": record.agent_id,
        "bench_version": record.bench_version,
        "state": record.state,
        "token": record.token,
        "amount_rao": record.amount_rao,
        "send_address": record.send_address,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if record.payment is not None:
        value.update(
            block_hash=record.payment.block_hash,
            block_number=record.payment.block_number,
            extrinsic_index=record.payment.extrinsic_index,
        )
    ledger[_ledger_key(network, hotkey, record.agent_id)] = value
    raw[READMISSION_LEDGER_BUCKET] = ledger
    return _save_preferences(raw)


def clear_readmission_record(
    *,
    network: str,
    hotkey: str,
    agent_id: str,
    payment: PaymentReceipt | None = None,
) -> bool:
    """Forget this agent's record. With ``payment``, only if it is that proof,
    so resolving one proof never discards a different one."""
    raw = _load_preferences()
    ledger = raw.get(READMISSION_LEDGER_BUCKET)
    key = _ledger_key(network, hotkey, agent_id)
    if not isinstance(ledger, dict) or key not in ledger:
        return True
    if payment is not None:
        current = _parse_record(ledger[key])
        if current is None or current.payment != payment:
            return True
    del ledger[key]
    raw[READMISSION_LEDGER_BUCKET] = ledger
    return _save_preferences(raw)
