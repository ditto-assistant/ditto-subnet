"""Append + read the hash-chained, append-only score audit log.

The audit log is the tamper-evident public projection of the k=3 scoring record.
Where ``scores`` is UPSERTed (only the current score survives a re-score), this
log is insert-only and ordered: it captures every scoring *event* as it happened,
including a re-test request that preserves the accepted row and an explicit
invalidation committed atomically with the operator-authorized replacement.

Tamper-evidence is a SHA-256 hash chain. Each entry's ``entry_hash`` is the
digest of its canonical JSON, which embeds ``prev_hash`` (the previous entry's
``entry_hash``; the genesis links to :data:`GENESIS_HASH`). Editing or removing
any historical entry changes its hash and breaks every later link, so a consumer
that replays the chain with :func:`verify_audit_chain` can prove the sequence was
never silently rewritten. Each ``score`` entry also carries the validator's
sr25519 signature verbatim, so authenticity (who scored) and integrity (nothing
reordered/dropped) are both independently checkable off the public read.

Appends run inside the caller's score-write transaction, so an entry is durable
iff its score is. A head lock (``SELECT ... FOR UPDATE`` on the latest row)
serializes concurrent appends onto one linear chain.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select

from ditto.db.models import ScoreAuditEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# The chain root: the first real entry's ``prev_hash``. 64 hex zeros (a value no
# SHA-256 digest of real content realistically collides with) marks "no parent".
GENESIS_HASH = "0" * 64

# Event kinds recorded in the log.
EVENT_SCORE = "score"
EVENT_FINALIZED = "agent_finalized"
# The reproduce-under-transform audit verdict (v3 Part A). Recorded whenever a
# finalized agent carried the metric, held or not, so the public feed shows the
# audit ran and what it found -- not only the failures. Its payload carries the
# PUBLIC inputs only (seed, block hash, robustness, pair counts) and never an
# answer key or a transformed expected answer, the same redaction rule the score
# entry follows: the verdict must be independently checkable from published data
# without the chain itself leaking the dataset's answers.
EVENT_AUDIT = "transform_audit"
EVENT_SCORE_INVALIDATED = "score_invalidated"
EVENT_SCORE_RETEST_QUEUED = "score_retest_queued"
EVENT_SCORE_RETEST_REQUESTED = "score_retest_requested"
EVENT_SCORE_RETEST_RELEASED = "score_retest_released"
EVENT_BENCHMARK_CONTRACT_REFRESH = "benchmark_contract_refresh"
EVENT_SCREENED_IMAGE_REBUILD = "screened_image_rebuild"

SCORE_RETEST_EVENTS = (
    EVENT_SCORE_RETEST_QUEUED,
    EVENT_SCORE_RETEST_REQUESTED,
    EVENT_SCORE_INVALIDATED,
    EVENT_SCORE_RETEST_RELEASED,
)


def benchmark_contract_refresh_event(bench_version: int) -> str:
    """Return the version-scoped durable authorization event for a rebuild."""
    return f"{EVENT_BENCHMARK_CONTRACT_REFRESH}:v{bench_version}"


def screened_image_rebuild_event(bench_version: int) -> str:
    """Return the version-scoped authorization event for an image rebuild."""
    return f"{EVENT_SCREENED_IMAGE_REBUILD}:v{bench_version}"


def _iso_utc(dt: datetime) -> str:
    """ISO-8601 in UTC, tolerant of a tz-naive value.

    The audit timestamp is part of the hash preimage, so append-time and
    verify-time must serialize it identically. A tz-naive value (the SQLite
    unit-test fallback drops the offset on round-trip) is treated as UTC, so the
    string is stable across a DB round-trip and across dialects.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _canonical_bytes(content: dict[str, Any]) -> bytes:
    """Deterministic JSON encoding used as the hash preimage.

    Sorted keys + no whitespace so the same content always hashes identically,
    across processes and languages — a third-party verifier in any stack can
    reproduce the digest.
    """
    return json.dumps(content, sort_keys=True, separators=(",", ":")).encode()


def compute_entry_hash(content: dict[str, Any]) -> str:
    """SHA-256 (hex) of an entry's canonical content (which embeds ``prev_hash``)."""
    return hashlib.sha256(_canonical_bytes(content)).hexdigest()


def _entry_content(
    *,
    agent_id: UUID,
    validator_hotkey: str | None,
    event: str,
    payload: dict[str, Any],
    recorded_at: datetime,
    prev_hash: str,
) -> dict[str, Any]:
    """Assemble the exact dict that is hashed into ``entry_hash``.

    Kept in one place so the append path and :func:`verify_audit_chain` hash
    byte-identical content.
    """
    return {
        "agent_id": str(agent_id),
        "validator_hotkey": validator_hotkey,
        "event": event,
        "payload": payload,
        "recorded_at": _iso_utc(recorded_at),
        "prev_hash": prev_hash,
    }


async def append_audit_entry(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str | None,
    event: str,
    payload: dict[str, Any],
    recorded_at: datetime,
) -> ScoreAuditEntry:
    """Append one immutable, hash-chained entry. Must run in a transaction.

    Locks the current chain head (``FOR UPDATE``) so concurrent appends serialize
    onto a single linear chain, links this entry to the head's ``entry_hash``,
    hashes the canonical content, and inserts. Returns the persisted row (its
    ``seq`` is assigned on flush).
    """
    head = (
        await session.execute(
            select(ScoreAuditEntry)
            .order_by(ScoreAuditEntry.seq.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()
    prev_hash = head.entry_hash if head is not None else GENESIS_HASH
    content = _entry_content(
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        event=event,
        payload=payload,
        recorded_at=recorded_at,
        prev_hash=prev_hash,
    )
    entry = ScoreAuditEntry(
        agent_id=agent_id,
        validator_hotkey=validator_hotkey,
        event=event,
        payload=payload,
        prev_hash=prev_hash,
        entry_hash=compute_entry_hash(content),
        recorded_at=recorded_at,
    )
    session.add(entry)
    await session.flush()
    return entry


async def list_audit_entries(
    session: AsyncSession, *, since_seq: int = 0, limit: int = 200
) -> list[ScoreAuditEntry]:
    """Return entries with ``seq > since_seq``, oldest first (page the chain).

    A consumer replays from ``since_seq=0`` and re-requests with the last ``seq``
    it saw to stream new entries, verifying links as it goes.
    """
    stmt = (
        select(ScoreAuditEntry)
        .where(ScoreAuditEntry.seq > since_seq)
        .order_by(ScoreAuditEntry.seq.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_audit_head(session: AsyncSession) -> ScoreAuditEntry | None:
    """Return the latest entry (the chain head), or ``None`` when the log is empty."""
    return (
        await session.execute(
            select(ScoreAuditEntry).order_by(ScoreAuditEntry.seq.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def get_latest_score_retest_event(
    session: AsyncSession,
    *,
    agent_id: UUID,
    validator_hotkey: str,
) -> ScoreAuditEntry | None:
    """Return the latest lifecycle event for one operator-requested re-test.

    A latest ``score_retest_queued`` entry means the original accepted score is
    canonical while the request waits behind that validator's current work. A
    latest ``score_retest_requested`` entry means the same validator holds a
    replacement ticket.
    ``score_invalidated`` closes it atomically when the replacement lands;
    ``score_retest_released`` closes it without changing the score.
    """
    return await session.scalar(
        select(ScoreAuditEntry)
        .where(
            ScoreAuditEntry.agent_id == agent_id,
            ScoreAuditEntry.validator_hotkey == validator_hotkey,
            ScoreAuditEntry.event.in_(SCORE_RETEST_EVENTS),
        )
        .order_by(ScoreAuditEntry.seq.desc())
        .limit(1)
    )


def verify_audit_chain(
    entries: list[ScoreAuditEntry], *, expected_prev: str = GENESIS_HASH
) -> bool:
    """Verify a contiguous run of entries: each hash recomputes and links.

    ``entries`` must be ordered by ascending ``seq``. Checks, for each entry,
    that (a) its ``prev_hash`` equals the running expected value (the prior
    entry's ``entry_hash``, or ``expected_prev`` for the first) and (b) its
    ``entry_hash`` recomputes from its stored content. Returns ``False`` on the
    first break. ``expected_prev`` lets a caller verify a mid-chain page by
    seeding it with the ``entry_hash`` of the entry just before the page.
    """
    running = expected_prev
    for entry in entries:
        if entry.prev_hash != running:
            return False
        content = _entry_content(
            agent_id=entry.agent_id,
            validator_hotkey=entry.validator_hotkey,
            event=entry.event,
            payload=entry.payload,
            recorded_at=entry.recorded_at,
            prev_hash=entry.prev_hash,
        )
        if compute_entry_hash(content) != entry.entry_hash:
            return False
        running = entry.entry_hash
    return True
