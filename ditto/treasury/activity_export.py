"""Read-only journal selections for public Backroom's verified receipt ingress.

This export is untrusted selection metadata, not proof or a signing operation.
Only Platform's independent finalized historical reader can publish a receipt.
"""

import json
import os
import sqlite3
import stat
import uuid
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Protocol


class JournalPolicy(Protocol):
    @property
    def digest(self) -> str: ...


def write_selector_snapshot(db: sqlite3.Connection, path: Path, policy: JournalPolicy):
    """Optional signer-owner hook: consistent allowlisted readonly export image.

    Run after the signer tick's transaction, using its already open connection.
    This does not mutate the journal. A private atomic DELETE-mode database lets
    the exporter read through a read-only mount even after WAL/SHM disappears.
    Never copy signed_json, call_json, delegate identities or event payloads.
    """
    if db.in_transaction:
        raise ValueError("snapshot requires completed signer transaction")
    directory = path.parent.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or directory.st_uid != os.geteuid()
        or (stat.S_IMODE(directory.st_mode) != 0o700)
    ):
        raise ValueError("snapshot requires private signer-owned directory")
    sources = [
        Path(row[2]).resolve() for row in db.execute("PRAGMA database_list") if row[2]
    ]
    protected = {
        Path(str(source) + suffix).resolve()
        for source in sources
        for suffix in ("", "-wal", "-shm", "-journal")
    }
    if path.resolve() in protected:
        raise ValueError("snapshot cannot replace authoritative journal or sidecar")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or (info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600)
        ):
            raise ValueError("snapshot file ownership or mode differs")
    temporary = path.with_name(".selector-snapshot-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    snapshot = sqlite3.connect(temporary)
    try:
        snapshot.execute("PRAGMA journal_mode=DELETE")
        snapshot.execute("PRAGMA synchronous=FULL")
        snapshot.executescript(
            "CREATE TABLE pin(digest,role);"
            "CREATE TABLE snapshot_meta(version,rows,last_operation);"
            "CREATE TABLE operations(id INTEGER PRIMARY KEY,role,state,"
            "source_block,bucket,"
            "amount,settlement_json);"
        )
        db.execute("BEGIN")
        try:
            pin = db.execute("SELECT digest,role FROM pin").fetchall()
            if len(pin) != 1 or tuple(pin[0]) != (policy.digest, "transfer"):
                raise ValueError("snapshot journal historical pin differs")
            expected = tuple(
                db.execute(
                    "SELECT count(*),COALESCE(max(id),0) FROM operations "
                    "WHERE role='transfer'"
                ).fetchone()
            )
            if expected[0] > 100000:
                raise ValueError("snapshot history exceeds explicit bounded export")
            snapshot.execute("INSERT INTO pin VALUES(?,'transfer')", (policy.digest,))
            cursor = db.execute(
                "SELECT id,role,state,source_block,bucket,amount,settlement_json "
                "FROM operations WHERE role='transfer' ORDER BY id"
            )
            count, last = 0, 0
            while chunk := cursor.fetchmany(1000):
                clean = []
                for row in chunk:
                    fields = list(row)
                    if type(row[0]) is not int or not last < row[0] < 2**63:
                        raise ValueError("snapshot operations unordered or invalid")
                    if row[2] == "finalized":
                        settlement = json.loads(row[6])
                        fields[6] = json.dumps(
                            {
                                key: settlement.get(key)
                                for key in (
                                    "status",
                                    "block",
                                    "block_hash",
                                    "extrinsic_index",
                                    "extrinsic_hash",
                                )
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    else:
                        fields[6] = None
                    clean.append(tuple(fields))
                    count, last = count + 1, row[0]
                snapshot.executemany(
                    "INSERT INTO operations VALUES(?,?,?,?,?,?,?)", clean
                )
            if (count, last) != expected:
                raise ValueError("snapshot source history inconsistent")
            snapshot.execute("INSERT INTO snapshot_meta VALUES(1,?,?)", expected)
            snapshot.commit()
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        snapshot.close()
        if any(
            Path(str(temporary) + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise ValueError("snapshot not fully committed in DELETE mode")
        fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory_fd = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        snapshot.close()
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def _export_finalized_distributions(
    path: Path,
    policy: JournalPolicy,
    epoch_at: Callable[[int], int],
    *,
    limit: int = 100,
    after_id: int = 0,
    snapshot_minimum=None,
    validate_history=None,
):
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(after_id) is not int
        or after_id < 0
    ):
        raise ValueError("bounded journal export required")
    directory, info = path.parent.lstat(), path.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or directory.st_mode & 0o077
        or info.st_mode & 0o077
        or directory.st_uid != os.geteuid()
        or info.st_uid != os.geteuid()
    ):
        raise ValueError("journal export requires private signer-owned regular file")
    # mode=ro retains WAL visibility, unlike immutable=1. Never instantiate the
    # writable CollectorJournal or select signed_json, call_json or event data.
    checkpoint = None
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("SELECT digest,role FROM pin").fetchone() != (
            policy.digest,
            "transfer",
        ):
            raise ValueError("journal policy or signer role differs")
        if snapshot_minimum is not None:
            if (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='snapshot_meta'"
                ).fetchone()
                is None
            ):
                raise ValueError(
                    "selector snapshot history lost, rolled back or changed"
                )
            mode = db.execute("PRAGMA journal_mode").fetchone()[0]
            meta = db.execute(
                "SELECT version,rows,last_operation FROM snapshot_meta"
            ).fetchall()
            actual = db.execute(
                "SELECT count(*),COALESCE(max(id),0) FROM operations "
                "WHERE role='transfer'"
            ).fetchone()
            if (
                mode != "delete"
                or len(meta) != 1
                or meta[0][0] != 1
                or tuple(meta[0][1:]) != tuple(actual)
                or actual[0] < snapshot_minimum[0]
                or actual[1] < snapshot_minimum[1]
                or actual[1] < after_id
            ):
                raise ValueError(
                    "selector snapshot history lost, rolled back or changed"
                )
            checkpoint = tuple(actual)
            if validate_history is not None:
                validate_history(db)
        # The reviewed signer permits one transfer in flight. Refuse a journal
        # that violates that ordering rather than skipping a late finalization
        # permanently when the observer advances its operation checkpoint.
        if db.execute(
            "SELECT 1 FROM operations WHERE role='transfer' AND id<=? "
            "AND state NOT IN ('finalized','failed','expired') LIMIT 1",
            (after_id,),
        ).fetchone():
            raise ValueError("journal transfer ordering requires explicit recovery")
        rows = db.execute(
            (
                "SELECT id,source_block,bucket,amount,settlement_json "
                "FROM operations WHERE role='transfer' AND "
                "state='finalized' AND source_block IS NOT NULL AND "
                "id>? AND id<COALESCE((SELECT MIN(id) FROM operations "
                "WHERE role='transfer' AND state NOT IN "
                "('finalized','failed','expired')),9223372036854775807) "
                "ORDER BY id LIMIT ?"
            ),
            (after_id, limit),
        ).fetchall()
    items = []
    for operation_id, source_block, bucket, amount, encoded in rows:
        settlement = json.loads(encoded)
        if settlement.get("status") != "finalized":
            raise ValueError("journal state and settlement conflict")
        if settlement.get("extrinsic_index") is None or not settlement.get(
            "extrinsic_hash"
        ):
            # Old journals need an independently found exact selector, never an
            # invented index or reading private signed payloads to reconstruct it.
            raise ValueError(
                "historical journal selector missing; independent recovery required"
            )
        items.append(
            {
                "journal_operation_id": operation_id,
                "stage": "service_distribution",
                "epoch_index": epoch_at(source_block),
                "bucket_id": bucket,
                "source_block": source_block,
                "block": settlement["block"],
                "block_hash": settlement["block_hash"],
                "extrinsic_index": settlement["extrinsic_index"],
                "extrinsic_hash": settlement["extrinsic_hash"],
                "amount_atomic": amount,
                "reason": "Observe finalized collector distribution",
            }
        )
    return items, checkpoint


def export_finalized_distributions(
    path: Path,
    policy: JournalPolicy,
    epoch_at: Callable[[int], int],
    *,
    limit=100,
    after_id=0,
) -> list[dict]:
    return _export_finalized_distributions(
        path, policy, epoch_at, limit=limit, after_id=after_id
    )[0]


def export_snapshot_distributions(
    path, policy, epoch_at, *, after_id, minimum, validate_history
):
    return _export_finalized_distributions(
        path,
        policy,
        epoch_at,
        after_id=after_id,
        snapshot_minimum=minimum,
        validate_history=validate_history,
    )
