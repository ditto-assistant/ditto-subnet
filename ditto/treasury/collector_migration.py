"""Offline journal transfer: preserve history, never reset spend or re-dispatch.

No import accesses cloud, chain, keys, environment or a journal. Migration
verifies the cold signature over the exact manifest digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path

from ditto.treasury.collector import CollectorPolicy, canonical
from ditto_screening_protocol.collector_receipts import (
    AUDITED_COLLECTOR_CODE_HASH,
    HISTORICAL_COLLECTOR_CODE_HASH,
)

SCHEMA = {
    "pin": ("digest", "role"),
    "cursor": ("id", "block"),
    "earnings": ("block", "amount", "block_hash", "event_digest", "completed"),
    "operations": (
        "id",
        "state",
        "role",
        "amount",
        "source_block",
        "bucket",
        "destination",
        "call_json",
        "signed_json",
        "at_block",
        "settlement_json",
        "reconciled_through",
    ),
    "events": ("id", "event", "payload"),
}


def policy_transition(old: CollectorPolicy, new: CollectorPolicy) -> None:
    allowed = {
        "revision",
        "gcp_project",
        "registration_delegate",
        "transfer_delegate",
        "registration_service_account",
        "transfer_service_account",
    }
    before, after = asdict(old), asdict(new)
    changed = {key for key in before if before[key] != after[key]}
    # Same isolated custody, fresh offline approval, exact audited runtime
    # transition. No wallet, budget, destination, start or interval may change.
    if (
        changed == {"revision", "runtime_code_hash"}
        and old.gcp_project == new.gcp_project == "sn118-gamma-custody"
        and new.revision == old.revision + 1
        and old.runtime_code_hash == HISTORICAL_COLLECTOR_CODE_HASH
        and new.runtime_code_hash == AUDITED_COLLECTOR_CODE_HASH
        and all(
            getattr(new, role + "_service_account")
            == f"sn118-collector-{role}@sn118-gamma-custody.iam.gserviceaccount.com"
            for role in ("registration", "transfer")
        )
    ):
        return
    if (
        changed != allowed
        or old.gcp_project != "ditto-app-dev"
        or new.gcp_project != "sn118-gamma-custody"
        or new.revision != old.revision + 1
    ):
        raise ValueError("unsupported custody-only policy transition")
    for role in ("registration", "transfer"):
        if (
            getattr(new, role + "_service_account")
            != f"sn118-collector-{role}@sn118-gamma-custody.iam.gserviceaccount.com"
        ):
            raise ValueError("new signer account not isolated")
    if {old.registration_delegate, old.transfer_delegate} & {
        new.registration_delegate,
        new.transfer_delegate,
    }:
        raise ValueError("fresh delegates required")


def private_parent(path: Path) -> None:
    for parent in (path.parent, *path.parent.parents):
        if parent.is_symlink():
            raise ValueError("symlink journal parent")
    info = path.parent.stat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("private owner directory required")


def snapshot(path: Path, expected_sha256: str) -> sqlite3.Connection:
    private_parent(path)
    if any(
        Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise ValueError("standalone quiesced snapshot required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not 100 <= info.st_size <= 64 * 1024 * 1024
        ):
            raise ValueError("private bounded snapshot required")
        with os.fdopen(fd, "rb", closefd=False) as source:
            raw = source.read(64 * 1024 * 1024 + 1)
    finally:
        os.close(fd)
    if (
        hashlib.sha256(raw).hexdigest() != expected_sha256
        or raw[:16] != b"SQLite format 3\x00"
        or raw[18:20] != b"\x01\x01"
    ):
        raise ValueError("snapshot hash or standalone format mismatch")
    db = sqlite3.connect(":memory:", isolation_level=None)
    try:
        db.deserialize(raw)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA trusted_schema=OFF")
        if [tuple(row) for row in db.execute("PRAGMA integrity_check")] != [("ok",)]:
            raise ValueError("snapshot integrity failed")
        objects = [
            tuple(row)
            for row in db.execute("SELECT type,name,tbl_name FROM sqlite_master")
        ]
        expected = {("table", name, name) for name in SCHEMA} | {
            ("index", "sqlite_autoindex_pin_1", "pin")
        }
        if set(objects) != expected or len(objects) != len(expected):
            raise ValueError("unexpected journal schema objects")
        for table, columns in SCHEMA.items():
            if (
                tuple(row["name"] for row in db.execute(f"PRAGMA table_info({table})"))
                != columns
            ):
                raise ValueError("journal schema columns changed")
        return db
    except BaseException:
        db.close()
        raise


def history(db: sqlite3.Connection) -> dict:
    return {
        table: [list(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in SCHEMA
        if table != "pin"
    }


def manifest(
    source: Path,
    old: CollectorPolicy,
    new: CollectorPolicy,
    role: str,
    source_sha256: str,
) -> dict:
    policy_transition(old, new)
    db = snapshot(source, source_sha256)
    try:
        return describe(db, old, new, role, source_sha256)
    finally:
        db.close()


def describe(
    db: sqlite3.Connection,
    old: CollectorPolicy,
    new: CollectorPolicy,
    role: str,
    source_sha256: str,
) -> dict:
    if role not in ("registration", "transfer") or [
        tuple(row) for row in db.execute("SELECT * FROM pin")
    ] != [(old.digest, role)]:
        raise ValueError("source policy or role mismatch")
    rows = [tuple(row) for row in db.execute("SELECT * FROM cursor")]
    if (
        len(rows) != 1
        or rows[0][0] != 1
        or type(rows[0][1]) is not int
        or not old.start_block - 1 <= rows[0][1] < 2**63
    ):
        raise ValueError("invalid preserved cursor")
    for row in db.execute("SELECT * FROM operations"):
        if (
            row["state"] not in ("finalized", "failed", "expired")
            or row["role"] != role
            or type(row["amount"]) is not int
            or row["amount"] < 0
        ):
            raise ValueError("unresolved or invalid source operation")
        settlement = json.loads(row["settlement_json"])
        if settlement.get("status") != row["state"]:
            raise ValueError("terminal settlement mismatch")
    retained = history(db)
    return {
        "schema": "ditto-collector-custody-migration-v1",
        "role": role,
        "from_policy_digest": old.digest,
        "to_policy_digest": new.digest,
        "source_sha256": source_sha256,
        "history_sha256": hashlib.sha256(canonical(retained).encode()).hexdigest(),
        "cursor": rows[0][1],
        "counts": {key: len(value) for key, value in retained.items()},
        "registration_reserved_rao": sum(row[3] for row in retained["operations"])
        if role == "registration"
        else 0,
    }


def migrate(
    source: Path,
    target: Path,
    old: CollectorPolicy,
    new: CollectorPolicy,
    approval: dict,
    signature: str,
) -> dict:
    """Verify cold approval; exclusive output is never replayed or overwritten."""
    from bittensor_wallet import Keypair

    if (
        not isinstance(approval, dict)
        or set(approval)
        != {
            "schema",
            "role",
            "from_policy_digest",
            "to_policy_digest",
            "source_sha256",
            "history_sha256",
            "cursor",
            "counts",
            "registration_reserved_rao",
        }
        or approval["schema"] != "ditto-collector-custody-migration-v1"
        or approval["role"] not in ("registration", "transfer")
        or not isinstance(approval["source_sha256"], str)
        or not isinstance(signature, str)
    ):
        raise ValueError("malformed migration approval envelope")
    try:
        signature_bytes = bytes.fromhex(signature.removeprefix("0x"))
    except ValueError:
        raise ValueError("invalid migration signature encoding") from None
    if len(signature_bytes) != 64:
        raise ValueError("invalid migration signature length")
    digest = hashlib.sha256(canonical(approval).encode()).hexdigest()
    if not Keypair(ss58_address=old.collector_coldkey).verify(
        f"ditto-collector-custody-migration-v1:{digest}".encode(),
        signature_bytes,
    ):
        raise ValueError("migration lacks exact cold approval")
    policy_transition(old, new)
    private_parent(target)
    db = snapshot(source, approval["source_sha256"])
    try:
        expected = describe(db, old, new, approval["role"], approval["source_sha256"])
        if canonical(approval) != canonical(expected):
            raise ValueError("cold-approved history manifest mismatch")
        retained = history(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE pin SET digest=?", (new.digest,))
        db.execute(
            "INSERT INTO events(event,payload) VALUES (?,?)",
            ("offline_approved_custody_migration", canonical(approval)),
        )
        db.execute("COMMIT")
        after = history(db)
        if (
            any(
                after[table] != rows
                for table, rows in retained.items()
                if table != "events"
            )
            or after["events"][:-1] != retained["events"]
        ):
            raise ValueError("history changed during migration")
        raw = db.serialize()
        # The final path appears only after the complete DB is durable. A hard
        # link publishes without replacing any existing path. If interruption
        # occurs after publication, preserve the complete target for review.
        fd, temporary_name = tempfile.mkstemp(
            prefix=".custody-migration-", dir=target.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, target, follow_symlinks=False)
            fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            **approval,
            "target_sha256": hashlib.sha256(raw).hexdigest(),
            "budget_reset": False,
            "history_deleted": False,
        }
    finally:
        db.close()
