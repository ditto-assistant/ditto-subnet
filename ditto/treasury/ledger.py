"""Local, append-only hash chain for read-only treasury rehearsals.

This is an operator artifact, not an on-chain receipt.  It deliberately cannot
record a payment as settled; an execution ledger needs independent finalized
chain and GM balance reconciliation before it may make that claim.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
ALLOWED_EVENTS = frozenset({"quote", "dry_run", "reconciliation_observation"})


def _digest(entry: dict[str, Any]) -> str:
    canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def verify(path: Path) -> tuple[int, str]:
    previous = GENESIS
    count = 0
    if not path.exists():
        return count, previous
    for raw in path.read_text().splitlines():
        entry = json.loads(raw)
        digest = entry.pop("hash")
        if entry.get("previous_hash") != previous or _digest(entry) != digest:
            raise ValueError(f"treasury rehearsal ledger breaks at entry {count + 1}")
        if entry.get("sequence") != count + 1:
            raise ValueError("treasury rehearsal ledger sequence is not contiguous")
        previous = digest
        count += 1
    return count, previous


def append(path: Path, *, event: str, payload: dict[str, Any]) -> dict[str, Any]:
    if event not in ALLOWED_EVENTS:
        raise ValueError("only read-only rehearsal events can be recorded")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("treasury rehearsal ledger must be a regular file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        sequence, previous = verify(path)
        entry = {
            "sequence": sequence + 1,
            "previous_hash": previous,
            "event": event,
            "payload": payload,
            "recorded_at": datetime.now(UTC).isoformat(),
        }
        entry["hash"] = _digest(entry)
        os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode())
        os.fsync(fd)
        return entry
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
