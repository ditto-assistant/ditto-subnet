"""SQLAlchemy 2.0 declarative ORM models for the Ditto data layer.

The alembic migrations in :file:`alembic/versions/` own the schema on
disk; these declarative models describe the same schema in Python so
SQLAlchemy can hydrate :class:`AsyncSession` queries into typed objects.
Models and migrations must stay in sync; future migrations can use
``alembic revision --autogenerate`` to draft from model diffs, with
manual review per migration to catch autogenerate's known footguns
(renames as DROP+ADD, partial-index handling, ENUM type changes).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    UUID as SaUUID,
)
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP


class Base(DeclarativeBase):
    """Declarative base for every Ditto ORM model.

    Carries the shared metadata so alembic's ``env.py`` can pass it to
    ``target_metadata`` for autogenerate workflows.
    """


class AgentStatus(StrEnum):
    """Lifecycle state machine values for the ``agents.status`` column.

    Matches the Postgres ENUM type ``agentstatus`` created in the
    initial-schema migration. :class:`enum.StrEnum` (Python 3.11+) makes
    values usable as plain strings so the SQLAlchemy ``Enum`` column can
    round-trip them through both the native PG ENUM type and SQLite's
    CHECK-constraint fallback used in unit tests.
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


class Agent(Base):
    """One row of the ``agents`` table.

    Represents a single miner submission. Lifecycle is tracked through
    :class:`AgentStatus`; transitions are owned by the upload, evaluator,
    and scoring modules.
    """

    __tablename__ = "agents"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    """Primary key. Client-generated UUID supplied at INSERT time."""

    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    """SS58 hotkey of the submitting miner."""

    name: Mapped[str] = mapped_column(Text, nullable=False)
    """Human-friendly agent name supplied by the miner."""

    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    """SHA-256 of the uploaded tarball, hex encoded."""

    status: Mapped[AgentStatus] = mapped_column(
        Enum(
            AgentStatus,
            name="agentstatus",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
            create_constraint=True,
        ),
        nullable=False,
        server_default=text("'uploaded'"),
    )
    """Current state in the submission state machine."""

    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Source IP of the upload request, for audit. ``NULL`` when not recorded."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """Upload timestamp (UTC)."""

    __table_args__ = (
        UniqueConstraint(
            "agent_id", "miner_hotkey", name="agents_agent_id_miner_hotkey_key"
        ),
        Index("agents_miner_hotkey_idx", "miner_hotkey"),
        Index(
            "agents_status_evaluating_idx",
            "status",
            postgresql_where=text("status = 'evaluating'"),
        ),
    )


class EvaluationPayment(Base):
    """One row of the ``evaluation_payments`` table.

    The composite primary key ``(block_hash, extrinsic_index)`` is the
    replay-protection mechanism: the same on-chain payment proof cannot
    be inserted twice. ``UNIQUE (agent_id)`` enforces the 1:1 invariant
    (one upload = one payment). The composite FK on
    ``(agent_id, miner_hotkey)`` documents the ownership invariant in
    DDL so future endpoints can't silently break it.
    """

    __tablename__ = "evaluation_payments"

    block_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """Hash of the block containing the payment extrinsic. PK part 1."""

    extrinsic_index: Mapped[int] = mapped_column(Integer, nullable=False)
    """Zero-based index of the extrinsic within the block. PK part 2."""

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """FK to ``agents.agent_id``. The agent this payment funds. ``UNIQUE``."""

    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    """Signer hotkey on the payment extrinsic. FK-bound to ``agents.miner_hotkey``."""

    miner_coldkey: Mapped[str] = mapped_column(Text, nullable=False)
    """Coldkey that owns the hotkey at payment time. Snapshot for audit."""

    amount_rao: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Payment amount in rao (1 TAO = 1e9 rao)."""

    dest_address: Mapped[str] = mapped_column(Text, nullable=False)
    """SS58 address that received the payment."""

    timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    """On-chain block timestamp."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """Row insertion timestamp."""

    __table_args__ = (
        PrimaryKeyConstraint(
            "block_hash", "extrinsic_index", name="evaluation_payments_pkey"
        ),
        UniqueConstraint("agent_id", name="evaluation_payments_agent_id_key"),
        ForeignKeyConstraint(
            ["agent_id", "miner_hotkey"],
            ["agents.agent_id", "agents.miner_hotkey"],
            ondelete="RESTRICT",
            name="evaluation_payments_agent_id_miner_hotkey_fkey",
        ),
        CheckConstraint("amount_rao > 0", name="evaluation_payments_amount_rao_check"),
        CheckConstraint(
            "extrinsic_index >= 0",
            name="evaluation_payments_extrinsic_index_check",
        ),
        Index("evaluation_payments_miner_hotkey_idx", "miner_hotkey"),
    )
