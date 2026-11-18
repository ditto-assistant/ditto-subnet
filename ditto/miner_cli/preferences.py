"""Small, human-editable miner CLI preferences.

Every write is a read-modify-write of the whole file, so every writer takes
one exclusive OS lock (``config.json.lock``) around the full sequence and
replaces the file atomically (temp file, fsync, ``os.replace``). Concurrent
CLI processes therefore never lose each other's updates, and a payment guard
written by ``ditto readmit`` is never erased by an unrelated preference write.
A file that exists but cannot be parsed is never overwritten.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ditto.miner_cli.errors import MinerCliError
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


class PreferencesUnreadableError(MinerCliError):
    """Raised when the preferences file exists but cannot be parsed.

    This can happen when:
    - The file was hand-edited into invalid JSON, or truncated by a full disk.
    - A readmission record in it is malformed, so the CLI cannot tell whether
      a payment was already made and refuses to act until it is repaired.
    """


def _read_preferences_strict() -> dict:
    """The parsed file; ``{}`` only when it does not exist yet."""
    path = preferences_path()
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise PreferencesUnreadableError(
            f"cannot read the CLI config {path}: {error}"
        ) from error
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise PreferencesUnreadableError(
            f"the CLI config {path} is not valid JSON ({error}); it was left "
            "untouched. Repair or move it, then rerun"
        ) from error
    if not isinstance(raw, dict):
        raise PreferencesUnreadableError(
            f"the CLI config {path} is not a JSON object; it was left untouched. "
            "Repair or move it, then rerun"
        )
    return raw


def _load_preferences() -> dict:
    """Lenient read for conveniences (names, sessions): unreadable is empty."""
    try:
        return _read_preferences_strict()
    except PreferencesUnreadableError:
        return {}


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def _exclusive(lock_file: Path, *, blocking: bool = True) -> Iterator[bool]:
    """Hold an exclusive ``flock`` on ``lock_file``; yields whether it was taken.

    ``flock`` is held per open file description, so it excludes other
    processes and other threads of this process alike, and the kernel drops
    it if the process dies.
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_atomic(path: Path, raw: dict) -> None:
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps(raw, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _update_preferences(mutate: Callable[[dict], bool]) -> bool:
    """Apply ``mutate`` under the preferences lock; return whether it persisted.

    ``mutate`` edits the freshly read document in place and returns whether
    it changed anything. An unparseable file is never overwritten.
    """
    path = preferences_path()
    try:
        with _exclusive(_lock_path(path)):
            try:
                raw = _read_preferences_strict()
            except PreferencesUnreadableError:
                return False
            if not mutate(raw):
                return True
            _write_atomic(path, raw)
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

    def _mutate(raw: dict) -> bool:
        names = raw.get("agent_names")
        if not isinstance(names, dict):
            names = {}
        names[_key(network=network, hotkey=hotkey)] = name
        raw["agent_names"] = names
        return True

    return _update_preferences(_mutate)


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

    def _mutate(raw: dict) -> bool:
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
        return True

    return _update_preferences(_mutate)


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

    def _mutate(raw: dict) -> bool:
        payments = raw.get(bucket)
        if not isinstance(payments, dict):
            payments = {}
        payments[
            _pending_payment_key(
                network=network, hotkey=hotkey, name=name, sha256=sha256
            )
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
        return True

    return _update_preferences(_mutate)


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
    def _mutate(raw: dict) -> bool:
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
        return True

    return _update_preferences(_mutate)


def clear_miner_session(*, network: str) -> bool:
    def _mutate(raw: dict) -> bool:
        sessions = raw.get("miner_sessions")
        if not isinstance(sessions, dict) or network not in sessions:
            return False
        del sessions[network]
        raw["miner_sessions"] = sessions
        return True

    return _update_preferences(_mutate)


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

    def _mutate(raw: dict) -> bool:
        payments = raw.get(bucket)
        if not isinstance(payments, dict):
            return False
        key = _pending_payment_key(
            network=network, hotkey=hotkey, name=name, sha256=sha256
        )
        current = payments.get(key)
        expected = {
            "block_hash": payment.block_hash,
            "block_number": payment.block_number,
            "extrinsic_index": payment.extrinsic_index,
        }
        if not isinstance(current, dict) or any(
            current.get(field) != value for field, value in expected.items()
        ):
            return False
        del payments[key]
        raw[bucket] = payments
        return True

    return _update_preferences(_mutate)


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


class ReadmissionBusyError(MinerCliError):
    """Raised when another ``ditto readmit`` holds this agent's payment lock.

    This can happen when:
    - Two terminals (or a script) run ``ditto readmit`` for the same agent at
      once. Only one may reserve and pay; the other refuses before anything.
    """


@contextmanager
def readmission_lock(*, network: str, hotkey: str, agent_id: str) -> Iterator[None]:
    """Hold this agent's readmission lock for a whole readmit run.

    The ledger record is read, written through ``reserved`` -> ``paying`` ->
    ``paid`` and resolved under this one lock, so two runs can never both see
    "no record" and both pay. A second run refuses instead of waiting, since
    the first may be sitting at its payment confirmation prompt.
    """
    digest = hashlib.sha256(
        _ledger_key(network, hotkey, agent_id).encode()
    ).hexdigest()[:32]
    lock_file = preferences_path().parent / "locks" / f"readmit-{digest}.lock"
    with _exclusive(lock_file, blocking=False) as taken:
        if not taken:
            raise ReadmissionBusyError(
                f"another `ditto readmit` for agent {agent_id} is running; "
                "nothing was sent by this run. Let it finish, then rerun"
            )
        yield


def _malformed(key: str, problem: str) -> PreferencesUnreadableError:
    return PreferencesUnreadableError(
        f"the saved readmission record {key!r} in {preferences_path()} is "
        f"malformed ({problem}). It may track a payment already made, so "
        "nothing was sent and the file was left untouched. Repair the record "
        "(or remove it only if you are sure no transfer was made), then rerun"
    )


def _receipt(value: dict, key: str) -> PaymentReceipt | None:
    fields = ("block_hash", "block_number", "extrinsic_index")
    present = [field for field in fields if value.get(field) is not None]
    if not present:
        return None
    block_hash = value.get("block_hash")
    block_number = value.get("block_number")
    extrinsic_index = value.get("extrinsic_index")
    if not (
        isinstance(block_hash, str)
        and block_hash.startswith("0x")
        and len(block_hash) == 66
        and isinstance(block_number, int)
        and not isinstance(block_number, bool)
        and block_number >= 1
        and isinstance(extrinsic_index, int)
        and not isinstance(extrinsic_index, bool)
        and extrinsic_index >= 0
    ):
        raise _malformed(
            key, "its proof needs block_hash, block_number and extrinsic_index"
        )
    return PaymentReceipt(
        block_hash=block_hash,
        block_number=block_number,
        extrinsic_index=extrinsic_index,
    )


def _parse_record(key: str, value: object) -> ReadmissionRecord:
    """Parse one saved record, failing closed: never "absent" when present."""
    if not isinstance(value, dict):
        raise _malformed(key, "not an object")
    agent_id = value.get("agent_id")
    bench_version = value.get("bench_version")
    state = value.get("state")
    if not isinstance(agent_id, str) or not key.endswith(f":{agent_id}"):
        raise _malformed(key, "agent_id is missing or does not match its key")
    if not isinstance(bench_version, int) or isinstance(bench_version, bool):
        raise _malformed(key, "bench_version is missing")
    if state not in ("reserved", "paying", "paid"):
        raise _malformed(key, f"unknown state {state!r}")
    payment = _receipt(value, key)
    if state == "paid" and payment is None:
        raise _malformed(key, "it is marked paid but has no proof")
    token = value.get("token")
    amount = value.get("amount_rao")
    address = value.get("send_address")
    updated = value.get("updated_at")
    if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool)):
        raise _malformed(key, "amount_rao is not an integer")
    if state == "paying" and (amount is None or not isinstance(address, str)):
        raise _malformed(
            key, "an unconfirmed transfer needs its amount_rao and send_address"
        )
    return ReadmissionRecord(
        agent_id=agent_id,
        bench_version=bench_version,
        state=state,
        token=token if isinstance(token, str) and token else None,
        amount_rao=amount,
        send_address=address if isinstance(address, str) else None,
        payment=payment,
        updated_at=updated if isinstance(updated, str) else "",
    )


def _ledger(raw: dict) -> dict:
    ledger = raw.get(READMISSION_LEDGER_BUCKET)
    if ledger is None:
        return {}
    if not isinstance(ledger, dict):
        raise PreferencesUnreadableError(
            f"the readmission ledger in {preferences_path()} is not an object; "
            "nothing was sent and the file was left untouched. Repair it, then rerun"
        )
    return ledger


def load_readmission_record(
    *, network: str, hotkey: str, agent_id: str
) -> ReadmissionRecord | None:
    """This agent's unresolved readmission payment, whatever era it was for.

    ``None`` only when there is genuinely no record. An unreadable file or a
    malformed record raises :class:`PreferencesUnreadableError`.
    """
    key = _ledger_key(network, hotkey, agent_id)
    value = _ledger(_read_preferences_strict()).get(key)
    return None if value is None else _parse_record(key, value)


def save_readmission_record(
    *, network: str, hotkey: str, record: ReadmissionRecord
) -> bool:
    """Atomically replace this agent's record; returns whether it persisted."""
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

    def _mutate(raw: dict) -> bool:
        ledger = raw.get(READMISSION_LEDGER_BUCKET)
        if ledger is not None and not isinstance(ledger, dict):
            raise PreferencesUnreadableError("readmission ledger is not an object")
        ledger = dict(ledger or {})
        ledger[_ledger_key(network, hotkey, record.agent_id)] = value
        raw[READMISSION_LEDGER_BUCKET] = ledger
        return True

    try:
        return _update_preferences(_mutate)
    except PreferencesUnreadableError:
        return False


def clear_readmission_record(
    *,
    network: str,
    hotkey: str,
    agent_id: str,
    payment: PaymentReceipt | None = None,
) -> bool:
    """Forget this agent's record; return whether it is gone afterwards.

    With ``payment``, only a record holding exactly that proof is removed, so
    resolving one proof never discards a different one (a different record is
    kept and this returns ``False``). A malformed record is never removed.
    """
    key = _ledger_key(network, hotkey, agent_id)
    gone = False

    def _mutate(raw: dict) -> bool:
        nonlocal gone
        ledger = raw.get(READMISSION_LEDGER_BUCKET)
        if not isinstance(ledger, dict) or key not in ledger:
            gone = ledger is None or isinstance(ledger, dict)
            return False
        if payment is not None:
            try:
                current = _parse_record(key, ledger[key])
            except PreferencesUnreadableError:
                return False
            if current.payment != payment:
                return False
        del ledger[key]
        raw[READMISSION_LEDGER_BUCKET] = ledger
        gone = True
        return True

    return _update_preferences(_mutate) and gone
