"""Small, human-editable miner CLI preferences."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ditto.miner_cli.models import PaymentReceipt


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
    *, network: str, hotkey: str, name: str, sha256: str
) -> PaymentReceipt | None:
    """Load a finalized proof saved for this exact local upload identity."""
    raw = _load_preferences()
    payments = raw.get("pending_upload_payments")
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


def save_pending_payment(
    *,
    network: str,
    hotkey: str,
    name: str,
    sha256: str,
    payment: PaymentReceipt,
) -> bool:
    """Persist a finalized proof before attempting the corresponding upload."""
    raw = _load_preferences()
    payments = raw.get("pending_upload_payments")
    if not isinstance(payments, dict):
        payments = {}
    payments[
        _pending_payment_key(network=network, hotkey=hotkey, name=name, sha256=sha256)
    ] = {
        "block_hash": payment.block_hash,
        "block_number": payment.block_number,
        "extrinsic_index": payment.extrinsic_index,
    }
    raw["pending_upload_payments"] = payments
    return _save_preferences(raw)


def clear_pending_payment(
    *,
    network: str,
    hotkey: str,
    name: str,
    sha256: str,
    payment: PaymentReceipt,
) -> bool:
    """Clear only the matching proof after a confirmed upload response."""
    raw = _load_preferences()
    payments = raw.get("pending_upload_payments")
    if not isinstance(payments, dict):
        return True
    key = _pending_payment_key(network=network, hotkey=hotkey, name=name, sha256=sha256)
    current = payments.get(key)
    expected = {
        "block_hash": payment.block_hash,
        "block_number": payment.block_number,
        "extrinsic_index": payment.extrinsic_index,
    }
    if current != expected:
        return True
    del payments[key]
    raw["pending_upload_payments"] = payments
    return _save_preferences(raw)
