"""Read-only journal selections for public Backroom's verified receipt ingress.

This export is untrusted selection metadata, not proof or a signing operation.
Only Platform's independent finalized historical reader can publish a receipt.
"""

import json
import os
import sqlite3
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class JournalPolicy(Protocol):
    @property
    def digest(self) -> str: ...


def export_finalized_distributions(
    path: Path,
    policy: JournalPolicy,
    epoch_at: Callable[[int], int],
    *,
    limit: int = 100,
    after_id: int = 0,
) -> list[dict]:
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
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        if db.execute("SELECT digest,role FROM pin").fetchone() != (
            policy.digest,
            "transfer",
        ):
            raise ValueError("journal policy or signer role differs")
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
    return items
