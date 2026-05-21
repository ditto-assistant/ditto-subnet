"""Frozen dataclass models for query results.

These are not ORM models. The schema is owned by the alembic migrations
in :file:`alembic/versions/`; ``models.py`` holds the Python-side
representation of rows the platform code reads. Each class provides a
``from_row`` classmethod that adapts an :class:`asyncpg.Record` into the
typed dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from asyncpg import Record


class AgentStatus(StrEnum):
    """Lifecycle state machine values for the ``agents.status`` column.

    Matches the Postgres ENUM type ``agentstatus`` created in the
    initial-schema migration. :class:`enum.StrEnum` (Python 3.11+) makes
    values usable as plain strings so asyncpg's text codec can round-trip
    them without a custom codec.
    """

    UPLOADED = "uploaded"
    SCREENING = "screening"
    SCREENING_PASSED = "screening_passed"
    SCREENING_FAILED = "screening_failed"
    EVALUATING = "evaluating"
    SCORED = "scored"
    LIVE = "live"
    ATH_PENDING_REVIEW = "ath_pending_review"
    BANNED = "banned"


@dataclass(frozen=True)
class Agent:
    """One row from the ``agents`` table.

    Represents a single miner submission. Lifecycle is tracked through
    :class:`AgentStatus`; transitions are owned by the upload, evaluator,
    and scoring modules.
    """

    agent_id: UUID
    """Primary key. Client-generated UUID supplied at INSERT time."""

    miner_hotkey: str
    """SS58 hotkey of the submitting miner. Indexed for per-miner lookups."""

    name: str
    """Human-friendly agent name supplied by the miner."""

    sha256: str
    """SHA-256 of the uploaded tarball, hex encoded."""

    status: AgentStatus
    """Current state in the submission state machine."""

    ip_address: str | None
    """Source IP of the upload request, for audit. ``NULL`` when not recorded."""

    created_at: datetime
    """Upload timestamp (UTC, ``TIMESTAMPTZ NOT NULL DEFAULT NOW()``)."""

    @classmethod
    def from_row(cls, row: Record) -> Agent:
        """Build an :class:`Agent` from an asyncpg row.

        Caller is responsible for ensuring the row contains every column
        named on the dataclass.
        """
        return cls(
            agent_id=row["agent_id"],
            miner_hotkey=row["miner_hotkey"],
            name=row["name"],
            sha256=row["sha256"],
            status=AgentStatus(row["status"]),
            ip_address=row["ip_address"],
            created_at=row["created_at"],
        )


@dataclass(frozen=True)
class EvaluationPayment:
    """One row from the ``evaluation_payments`` table.

    The composite primary key ``(block_hash, extrinsic_index)`` is the
    replay-protection mechanism: the same on-chain payment proof cannot
    be inserted twice. A duplicate-insert attempt surfaces as
    :class:`IntegrityError` for the caller to translate into the
    appropriate API response.
    """

    block_hash: str
    """Hash of the block containing the payment extrinsic. PK part 1."""

    extrinsic_index: int
    """Zero-based index of the extrinsic within the block. PK part 2."""

    agent_id: UUID
    """FK to ``agents.agent_id``. The agent this payment funds. ``UNIQUE``."""

    miner_hotkey: str
    """Signer hotkey on the payment extrinsic. FK-bound to ``agents.miner_hotkey``."""

    miner_coldkey: str
    """Coldkey that owns the hotkey at payment time. Snapshot for audit."""

    amount_rao: int
    """Payment amount in rao (1 TAO = 1e9 rao). ``CHECK (amount_rao > 0)``."""

    dest_address: str
    """SS58 address that received the payment."""

    timestamp: datetime
    """On-chain block timestamp."""

    created_at: datetime
    """Row insertion timestamp (``TIMESTAMPTZ NOT NULL DEFAULT NOW()``)."""

    @classmethod
    def from_row(cls, row: Record) -> EvaluationPayment:
        """Build an :class:`EvaluationPayment` from an asyncpg row."""
        return cls(
            block_hash=row["block_hash"],
            extrinsic_index=row["extrinsic_index"],
            agent_id=row["agent_id"],
            miner_hotkey=row["miner_hotkey"],
            miner_coldkey=row["miner_coldkey"],
            amount_rao=row["amount_rao"],
            dest_address=row["dest_address"],
            timestamp=row["timestamp"],
            created_at=row["created_at"],
        )
