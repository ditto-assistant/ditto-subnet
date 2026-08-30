"""SQLAlchemy 2.0 declarative models for the Ditto data layer.

Alembic migrations under :file:`alembic/versions/` own the schema;
these models describe it in Python so :class:`AsyncSession` queries
hydrate into typed objects. Models and migrations must stay in sync.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    Float,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import (
    UUID as SaUUID,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.ticket_status import TicketPurpose, TicketStatus

# Per-case detail is a JSON blob: JSONB on Postgres (indexable, compact),
# plain JSON on the SQLite unit-test fallback. The variant keeps one model
# working across both dialects.
_JSON_VARIANT = JSON().with_variant(JSONB(), "postgresql")
_NULLABLE_JSON_VARIANT = JSON(none_as_null=True).with_variant(
    JSONB(none_as_null=True), "postgresql"
)

# Naming convention so alembic autogenerate produces deterministic constraint
# names instead of random SHA suffixes.
_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every Ditto ORM model."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


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

    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Immutable revision within ``(miner_hotkey, name)``; null for legacy rows."""

    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    """SHA-256 of the uploaded tarball, hex encoded."""

    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Uploaded tarball size in bytes. Nullable for rows written before the
    ledger migration; a cheap near-dup signal (a copy has a near-identical size)."""

    # The four anti-copy sketch columns below hold multi-hundred-KB JSON blobs
    # (k=256 minhash arrays, embedding vectors). They are deferred behind the
    # "anticopy" group: the dominant DB cost in production was serializing them
    # on every Agent read (leaderboard/ticket/screener paths that never look at
    # them). Readers that DO need them — the scoring gate, the admin copy-review
    # comparison, the fingerprint backfill — load with
    # ``undefer_group("anticopy")`` / ``include_anticopy=True``; under the async
    # session a forgotten undefer fails loudly (MissingGreenlet) rather than
    # silently re-fetching.
    content_fingerprint: Mapped[dict | None] = mapped_column(
        _JSON_VARIANT, nullable=True, deferred=True, deferred_group="anticopy"
    )

    """Shingle MinHash sketch of the tarball source (see
    :mod:`ditto.api_server.fingerprint`). Feeds the anti-copy gate's content-level
    signal: a reindented/renamed/reformatted or locally-edited copy keeps a
    near-identical fingerprint even when its byte size drifts past the
    ``size_bytes`` tolerance. Nullable for rows written before this landed and for
    tarballs that were unreadable/empty at upload (the gate reads null as "no
    content match")."""

    structural_fingerprint: Mapped[dict | None] = mapped_column(
        _JSON_VARIANT, nullable=True, deferred=True, deferred_group="anticopy"
    )
    """AST-level shingle MinHash sketch of the crate, same ``{v,k,card,m}`` shape as
    :attr:`content_fingerprint` (see the dittobench ``astfp`` package). Computed by
    dittobench where the crate is unpacked and a Rust parser exists, it hashes only
    the parse-tree *shape*, so it survives identifier renaming that the lexical
    channel misses. Arrives (unsigned, advisory) on the score report and is written
    at score time. Nullable: null before this landed, on the local harness_url
    path, or when the crate has no parseable Rust."""

    normalized_source_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """SHA-256 (hex) of the crate's canonicalized source — comments stripped,
    whitespace removed, files sorted (see
    :func:`ditto.api_server.fingerprint.compute_normalized_source_hash`). Feeds the
    anti-copy gate's "exact-repack" signal: an *equality* match means the same
    source repackaged (reformat / re-comment / file rename+reorder), which the
    ``sha256`` and shingle sketches miss. Computed at upload. Nullable for rows
    written before this landed and for tarballs unreadable/empty at upload (the
    gate reads null as "no repack match")."""

    dataset_seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """The per-submission dataset seed the platform generated at job-ready
    (``uploaded -> evaluating``). All k=3 validators score against THIS seed, so
    their scores are comparable (the median-of-3 is over one dataset). Fresh and
    unpredictable per submission, so a published run's answer key does not help the
    miner's next (differently-seeded) submission. Null until the agent is promoted
    to ``evaluating``. Bounded to the signed 64-bit range ``scores.seed`` stores."""

    dataset_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    """SHA-256 (hex) of the fully-rendered dataset the generate service produced
    for ``dataset_seed`` (the DatasetArtifact digest). Issued to every validator in
    the ticket; the validator's scoring call regenerates the dataset from the seed
    and the scoring API fails if it does not hash to this — tamper-evidence that
    all three validators scored the exact dataset the platform pinned. Null until
    job-ready."""

    dataset_run_size: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The generator profile the dataset was built with (``small|medium|full``),
    issued with the ticket so the validator's scoring call uses the same profile.
    Null until job-ready."""

    dataset_seed_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Number of the on-chain block whose hash the ``dataset_seed`` was derived
    from (see :mod:`ditto.api_server.onchain_seed`). Pinned at job-ready so anyone
    can fetch that block, recompute ``derive_seed(block_hash, agent_id)``, and
    verify the seed the platform published — the seed is not platform-chosen. Null
    until job-ready, or when chain-derivation is unavailable (fallback path)."""

    dataset_seed_block_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Hex hash of :attr:`dataset_seed_block`, stored alongside the number so a
    verifier need not trust the platform's block lookup: it recomputes the seed
    directly from this hash + the agent id. Null until job-ready / on fallback."""

    code_embedding: Mapped[list | None] = mapped_column(
        _JSON_VARIANT, nullable=True, deferred=True, deferred_group="anticopy"
    )
    """Unit-norm code-embedding vector (JSON float array) of the crate's canonical
    source, from the self-hosted the code-embedding signal embedding service (see
    :mod:`ditto.api_server.embedding`). The rename/refactor-robust anti-copy signal:
    a code embedder scores a renamed+refactored copy high but a genuinely different
    agent low, so it is orthogonal to same-harness convergence. Stored in shadow
    mode — computed for every agent (calibration + retroactive) but not yet a hold
    trigger. Nullable before this landed, when the embedder is disabled
    (``CODE_EMBEDDER_URL`` unset), or on any best-effort embed failure."""

    code_embed_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``model@revision`` provenance tag of :attr:`code_embedding` (see
    :attr:`ditto.api_server.embedding.EmbeddingConfig.model_tag`). Lets a model
    change drive a re-embed sweep, and lets the gate compare only same-model vectors
    (a cross-model cosine is meaningless). Nullable alongside the vector."""

    prompt_fingerprint: Mapped[dict | None] = mapped_column(
        _JSON_VARIANT, nullable=True, deferred=True, deferred_group="anticopy"
    )
    """Word-shingle MinHash sketch of the crate's prompt-length string literals
    (see :func:`ditto.api_server.fingerprint.compute_prompt_fingerprint`), same
    ``{v,k,card,m}`` shape as :attr:`content_fingerprint` but with a string ``v`` so
    it never compares against the lexical/structural channels. The prompt
    surface: because it hashes string *contents*, it survives identifier renaming
    that defeats the lexical + normalized-source channels. Computed at upload.
    Stored in shadow mode — captured for every agent (calibration + retroactive
    analysis) but not yet a hold trigger on its own, since honest agents on the same
    reference harness share scaffolding prompts and the orthogonal-to-convergence
    signals it must fuse with are not built yet. Nullable before this landed / no
    prompt-length literal / unreadable tarball."""

    duplicate_of: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    """Set when the anti-copy gate holds this agent in ``ath_pending_review``:
    the ``agent_id`` of the earlier submission it appears to duplicate."""

    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Human-readable reason a held agent was routed to review (audit trail)."""

    screening_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Public-safe reason category for a failed build/serve screen.

    The screener's raw ``detail`` may contain an untrusted Docker build-log tail,
    so the endpoint maps it to a fixed category before persisting it here.
    """

    screening_reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Stable public/operator-safe machine code for the screening outcome."""

    screening_policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    """Latest anti-cheat screening policy this submission passed.

    Zero marks submissions screened before policy attestation was introduced.
    Validators may score only submissions at the platform's required version.
    """

    screened_image_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    """SHA-256 of the screener-exported Docker image archive."""

    screened_image_size_bytes: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    """Exact byte size of the screener-exported Docker image archive."""

    screened_image_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Docker content ID verified by the screener and each validator."""

    screened_image_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Screener-owned tag expected inside the Docker save archive."""

    screened_image_upload_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    """Platform-minted immutable multipart object identity."""

    screened_image_verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    """When the platform streamed and verified the complete archive bytes."""

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
        # Unconditional, exactly as 2026_07_15_add_agent_versions.py creates
        # it. This used to carry `.ddl_if(dialect="postgresql")`, which was
        # only ever an accommodation for the SQLite test tier: the gate made
        # `create_all` skip it, so tests could seed several simultaneous
        # agents sharing one `(hotkey, name, version)` identity -- rows
        # production has always forbidden. #446 deleted that tier; nothing
        # builds this schema on SQLite any more, so the gate is dead.
        UniqueConstraint(
            "miner_hotkey",
            "name",
            "version",
            name="agents_hotkey_name_version_key",
        ),
        CheckConstraint(
            "version IS NULL OR version > 0", name="agents_version_positive_check"
        ),
        CheckConstraint(
            "(screened_image_sha256 IS NULL AND screened_image_size_bytes IS NULL "
            "AND screened_image_id IS NULL AND screened_image_ref IS NULL "
            "AND screened_image_upload_id IS NULL "
            "AND screened_image_verified_at IS NULL) OR "
            "(length(screened_image_sha256) = 64 AND screened_image_size_bytes > 0 "
            "AND length(screened_image_id) = 71 AND length(screened_image_ref) > 0 "
            "AND screened_image_upload_id IS NOT NULL "
            "AND screened_image_verified_at IS NOT NULL)",
            name="agents_screened_image_fields_check",
        ),
        Index("agents_miner_hotkey_idx", "miner_hotkey"),
        Index("agents_sha256_idx", "sha256"),
        Index("agents_created_agent_idx", "created_at", "agent_id"),
        # Exact-repack duplicate lookups for the quarantine review console.
        Index("agents_normalized_source_hash_idx", "normalized_source_hash"),
        Index(
            "agents_status_evaluating_idx",
            "status",
            postgresql_where=text("status = 'evaluating'"),
        ),
        # Screener polls for agents in the 'uploaded' state; a partial index
        # keeps that lookup cheap (mirrors the evaluating index).
        Index(
            "agents_status_uploaded_idx",
            "status",
            postgresql_where=text("status = 'uploaded'"),
        ),
        # The validator's ledger read (GET /scoring/scores) selects agents in
        # 'scored'; a partial index keeps that scan cheap (mirrors the two above).
        Index(
            "agents_status_scored_idx",
            "status",
            postgresql_where=text("status = 'scored'"),
        ),
        # ``duplicate_of`` points at the earlier submission a held agent copies.
        ForeignKeyConstraint(
            ["duplicate_of"],
            ["agents.agent_id"],
            ondelete="SET NULL",
            name="agents_duplicate_of_fkey",
        ),
    )


class ScreenedImageUpload(Base):
    """Attempt-bound multipart upload verified before a passing verdict."""

    __tablename__ = "screened_image_uploads"

    image_upload_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True
    )
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    screener_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    storage_upload_id: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    image_id: Mapped[str] = mapped_column(Text, nullable=False)
    image_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="initiated"
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="screened_image_uploads_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
            name="screened_image_uploads_attempt_id_fkey",
        ),
        CheckConstraint(
            "status IN ('initiated', 'verified', 'aborted')",
            name="screened_image_uploads_status_check",
        ),
        CheckConstraint("size_bytes > 0", name="screened_image_uploads_size_check"),
        Index("screened_image_uploads_attempt_idx", "attempt_id"),
        Index("screened_image_uploads_status_expires_idx", "status", "expires_at"),
    )


class ScreeningAttempt(Base):
    """One claimed, versioned screening lease for a submission."""

    __tablename__ = "screening_attempts"

    attempt_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    screener_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    public_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_lane: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    private_failure_log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_captured_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    duplicate_of: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    build_only: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    review_settings_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    review_settings_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_settings_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_settings_checksum: Mapped[str | None] = mapped_column(Text, nullable=True)
    """This attempt only rebuilds an already-adjudicated submission's missing
    prerequisites (screened image / dataset); the screener must NOT re-run the
    anti-cheat source review and cannot quarantine. Set when an EVALUATING agent
    on the current policy is re-claimed — its review was already cleared, so a
    re-screen would wrongly re-judge an approved artifact."""

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="screening_attempts_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["duplicate_of"],
            ["agents.agent_id"],
            ondelete="SET NULL",
            name="screening_attempts_duplicate_of_fkey",
        ),
        CheckConstraint(
            "policy_version > 0",
            name="screening_attempts_policy_version_check",
        ),
        CheckConstraint(
            "status IN ('running', 'passed', 'rejected', 'failed', 'expired', "
            "'quarantined')",
            name="screening_attempts_status_check",
        ),
        CheckConstraint(
            "deadline >= started_at",
            name="screening_attempts_deadline_check",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="screening_attempts_finished_check",
        ),
        CheckConstraint(
            # Verbatim from 2026_07_15_add_exact_duplicate_precheck.py, which
            # creates this constraint *name* with this predicate. `models.py`
            # used to declare the same name as `length(reason_code) BETWEEN 1
            # AND 64` -- strictly weaker, so tests accepted reason codes
            # production rejects (`suspicious_source` with an underscore was
            # one, and #446 had to hyphenate three fixtures).
            "reason_code IS NULL OR reason_code ~ '^[a-z0-9][a-z0-9-]{0,63}$'",
            name="screening_attempts_reason_code_check",
        ),
        CheckConstraint(
            "(review_settings_revision IS NULL "
            "AND review_settings_instance_id IS NULL "
            "AND review_settings_scope IS NULL "
            "AND review_settings_checksum IS NULL) OR "
            "(review_settings_revision > 0 "
            "AND review_settings_instance_id IS NOT NULL "
            "AND review_settings_scope IS NOT NULL "
            "AND length(review_settings_checksum) = 64)",
            name="screening_attempts_review_settings_binding_check",
        ),
        Index("screening_attempts_agent_started_idx", "agent_id", "started_at"),
        Index(
            "screening_attempts_one_running_idx",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )


class AthReview(Base):
    """Durable, immutable-evidence audit record for an ATH copy hold."""

    __tablename__ = "ath_reviews"

    review_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    opened_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    reopened_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolution: Mapped[str | None] = mapped_column(Text)
    resolution_reason: Mapped[str | None] = mapped_column(Text)
    original_duplicate_of: Mapped[UUID | None] = mapped_column(SaUUID(as_uuid=True))
    original_reason: Mapped[str | None] = mapped_column(Text)
    original_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    original_evidence: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    algorithm_provenance: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["original_duplicate_of"], ["agents.agent_id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("agent_id", name="ath_reviews_agent_id_key"),
        CheckConstraint(
            "status IN ('pending', 'resolved')", name="ath_reviews_status_check"
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('clear', 'reject')",
            name="ath_reviews_resolution_check",
        ),
        CheckConstraint(
            "(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL "
            "AND resolution IS NULL AND resolution_reason IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL "
            "AND resolved_by IS NOT NULL "
            "AND length(trim(resolved_by)) BETWEEN 1 AND 120 "
            "AND resolution IS NOT NULL "
            "AND resolution IN ('clear', 'reject') "
            "AND resolution_reason IS NOT NULL "
            "AND length(trim(resolution_reason)) >= 3)",
            name="ath_reviews_lifecycle_check",
        ),
        Index("ath_reviews_status_opened_idx", "status", "opened_at", "review_id"),
    )


class AthReviewAction(Base):
    """Append-only operator lifecycle history for an ATH review."""

    __tablename__ = "ath_review_actions"

    action_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    review_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["review_id"], ["ath_reviews.review_id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "action IN ('reopen', 'clear', 'reject')",
            name="ath_review_actions_action_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 3",
            name="ath_review_actions_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="ath_review_actions_actor_check",
        ),
        Index(
            "ath_review_actions_review_created_idx",
            "review_id",
            "created_at",
            "action_id",
        ),
    )


class ScreeningRetryOverride(Base):
    """Append-only operator grant that waives one failed attempt's backoff."""

    __tablename__ = "screening_retry_overrides"

    override_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    expected_score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    force_full_review: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["attempt_id"], ["screening_attempts.attempt_id"], ondelete="CASCADE"
        ),
        UniqueConstraint("attempt_id", name="screening_retry_overrides_attempt_key"),
        CheckConstraint(
            "length(artifact_sha256) = 64",
            name="screening_retry_overrides_sha_length_check",
        ),
        CheckConstraint(
            "expected_score_count >= 0",
            name="screening_retry_overrides_score_count_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screening_retry_overrides_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screening_retry_overrides_actor_check",
        ),
        Index(
            "screening_retry_overrides_agent_created_idx",
            "agent_id",
            "created_at",
            "override_id",
        ),
    )


class ScreeningQuarantine(Base):
    """Append-only quarantine decision plus its operator resolution."""

    __tablename__ = "screening_quarantines"

    quarantine_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    screener_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_digest: Mapped[str] = mapped_column(Text, nullable=False)
    finding_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_code: Mapped[str] = mapped_column(Text, nullable=False)
    review_audit_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Digest of the signed, public-safe reviewer budget audit."""

    review_audit: Mapped[dict | None] = mapped_column(
        _NULLABLE_JSON_VARIANT, nullable=True
    )
    """Typed reviewer budget/accounting audit; never source or prompt content."""

    evidence: Mapped[list | None] = mapped_column(_JSON_VARIANT, nullable=True)
    """Bounded public-safe policy evidence trail (module, code, summary,
    digest) shipped by the screener on quarantine. Display data for the
    operator console; the signed verdict binds only the digests. Null for
    rows written before the review payloads landed."""

    finding: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    """Bounded source-review finding (risk, confidence, categories, flagged
    path/line evidence, summary). Its canonical JSON hashes to
    ``finding_digest``, which the verdict signature covers, so this payload is
    verifiable end to end. Null before the review payloads landed and for
    quarantines with no source-review finding."""

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("attempt_id", name="screening_quarantines_attempt_id_key"),
        CheckConstraint(
            "policy_version > 0", name="screening_quarantines_policy_check"
        ),
        # The three digest/reason-code formats below are declared inline on the
        # columns by 2026_07_14_add_screening_quarantines.py, so Postgres named
        # them itself and `models.py` never carried them. Production has always
        # enforced all three; declaring them here is what lets a test see the
        # schema production actually has.
        CheckConstraint(
            "manifest_digest ~ '^[0-9a-f]{64}$'",
            name="screening_quarantines_manifest_digest_check",
        ),
        CheckConstraint(
            "finding_digest IS NULL OR finding_digest ~ '^[0-9a-f]{64}$'",
            name="screening_quarantines_finding_digest_check",
        ),
        CheckConstraint(
            "review_audit_digest IS NULL OR review_audit_digest ~ '^[0-9a-f]{64}$'",
            name="screening_quarantines_review_audit_digest_check",
        ),
        CheckConstraint(
            "(review_audit IS NULL) = (review_audit_digest IS NULL)",
            name="screening_quarantines_review_audit_pair_check",
        ),
        CheckConstraint(
            "review_audit IS NULL OR reason_code IN "
            "('source-review-inconclusive', 'agentic-source-review-tripwire')",
            name="screening_quarantines_review_audit_reason_check",
        ),
        CheckConstraint(
            "reason_code ~ '^[a-z0-9][a-z0-9-]{0,63}$'",
            name="screening_quarantines_reason_code_check",
        ),
        CheckConstraint(
            "status IN ('active', 'resolved')",
            name="screening_quarantines_status_check",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('release', 'rescreen', 'reject')",
            name="screening_quarantines_resolution_check",
        ),
        Index(
            "screening_quarantines_one_active_agent_idx",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index("screening_quarantines_created_idx", "created_at"),
        # Miner-history lookups (all quarantines for one agent, any status).
        Index("screening_quarantines_agent_idx", "agent_id"),
    )


class ScreeningQuarantineResolution(Base):
    """Append-only operator action history for a screening quarantine."""

    __tablename__ = "screening_quarantine_resolutions"

    resolution_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    quarantine_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    resolution: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["quarantine_id"],
            ["screening_quarantines.quarantine_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "resolution IN ('release', 'rescreen', 'reject')",
            name="screening_quarantine_resolutions_resolution_check",
        ),
        Index(
            "screening_quarantine_resolutions_quarantine_created_idx",
            "quarantine_id",
            "created_at",
        ),
    )


class ScreeningDispute(Base):
    """One miner-authenticated appeal of a rejected screening decision."""

    __tablename__ = "screening_disputes"

    dispute_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    quarantine_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["quarantine_id"],
            ["screening_quarantines.quarantine_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("agent_id", name="screening_disputes_agent_id_key"),
        UniqueConstraint("quarantine_id", name="screening_disputes_quarantine_id_key"),
        CheckConstraint(
            "length(message) BETWEEN 20 AND 1000",
            name="screening_disputes_message_length_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="screening_disputes_status_check",
        ),
        CheckConstraint(
            "resolution IS NULL OR resolution IN ('release', 'uphold')",
            name="screening_disputes_resolution_check",
        ),
        Index("screening_disputes_status_created_idx", "status", "created_at"),
    )


class EvaluationPayment(Base):
    """One row of the ``evaluation_payments`` table.

    The composite primary key ``(block_hash, extrinsic_index)`` is the
    replay-protection mechanism: the same on-chain payment proof cannot
    be inserted twice. An assigned row has ``agent_id`` set; a payment made for
    an accidental byte-identical upload instead has ``credit_for_agent_id`` set
    until it is atomically consumed by a different artifact. The XOR constraint
    makes those states exclusive. ``UNIQUE (agent_id)`` preserves one payment
    per evaluated upload, while the composite FK on ``(agent_id, miner_hotkey)``
    documents the ownership invariant in DDL.
    """

    __tablename__ = "evaluation_payments"

    block_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """Hash of the block containing the payment extrinsic. PK part 1."""

    extrinsic_index: Mapped[int] = mapped_column(Integer, nullable=False)
    """Zero-based index of the extrinsic within the block. PK part 2."""

    agent_id: Mapped[UUID | None] = mapped_column(SaUUID(as_uuid=True), nullable=True)
    """FK to ``agents.agent_id`` when the payment has funded an evaluation.

    ``NULL`` means the payment is a reusable credit created after an accidental
    byte-identical upload. The proof remains replay-protected by this row and is
    atomically assigned when the miner submits a different artifact.
    """

    credit_for_agent_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    """Earlier same-owner agent whose identical artifact created this credit."""

    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    """Signer hotkey on the payment extrinsic. FK-bound to ``agents.miner_hotkey``."""

    miner_coldkey: Mapped[str] = mapped_column(Text, nullable=False)
    """Coldkey that owns the hotkey at payment time. Snapshot for audit."""

    amount_rao: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Payment amount in rao (1 TAO = 1e9 rao)."""

    accepted_under_legacy_fee_amnesty: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    """Whether a pre-cutover reservation accepted its finalized legacy amount."""

    tao_usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    """TAO/USD oracle rate used at verification; null only for legacy rows."""

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
        ForeignKeyConstraint(
            ["credit_for_agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="evaluation_payments_credit_for_agent_id_fkey",
        ),
        CheckConstraint(
            "(agent_id IS NOT NULL) <> (credit_for_agent_id IS NOT NULL)",
            name="evaluation_payments_assignment_xor_credit",
        ),
        CheckConstraint("amount_rao > 0", name="evaluation_payments_amount_rao_check"),
        CheckConstraint(
            "tao_usd_rate IS NULL OR tao_usd_rate > 0",
            name="evaluation_payments_tao_usd_rate_positive",
        ),
        CheckConstraint(
            "extrinsic_index >= 0",
            name="evaluation_payments_extrinsic_index_check",
        ),
        Index("evaluation_payments_miner_hotkey_idx", "miner_hotkey"),
        Index(
            "evaluation_payments_available_credit_idx",
            "miner_hotkey",
            postgresql_where=text("agent_id IS NULL"),
        ),
    )


class CodingCapabilityCertification(Base):
    """One immutable validator-signed shadow coding capability receipt.

    Rows are append-only and never participate in score aggregation or weights.
    Active certification is derived at read time from the exact agent/source
    artifact, screened-image digest, ``certified`` status, and expiry.
    """

    __tablename__ = "coding_capability_certifications"

    certification_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    screened_image_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_deadline: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    coding_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    certification_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    failure_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    certification_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    canary_manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    transcript_object_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    frozen_submission_object_key: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    receipt: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="coding_certifications_agent_id_fkey",
        ),
        UniqueConstraint(
            "agent_id",
            "validator_hotkey",
            "coding_contract_version",
            "certification_id",
            name="coding_certifications_identity_key",
        ),
        CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_artifact_sha_check",
        ),
        CheckConstraint(
            "screened_image_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_image_sha_check",
        ),
        CheckConstraint(
            "certification_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_receipt_sha_check",
        ),
        CheckConstraint(
            "canary_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_certifications_canary_sha_check",
        ),
        CheckConstraint(
            "signature ~ '^[0-9a-f]{128}$'",
            name="coding_certifications_signature_check",
        ),
        CheckConstraint(
            "coding_contract_version > 0 AND bench_version > 0",
            name="coding_certifications_versions_positive",
        ),
        CheckConstraint(
            "status IN ('unsupported', 'failed', 'certified')",
            name="coding_certifications_status_check",
        ),
        CheckConstraint(
            "weight_eligible = false",
            name="coding_certifications_weight_ineligible",
        ),
        CheckConstraint(
            "expires_at > issued_at AND expires_at <= issued_at + interval '24 hours'",
            name="coding_certifications_expiry_check",
        ),
        CheckConstraint(
            "((status = 'certified' AND failure_stage IS NULL "
            "AND failure_code IS NULL) "
            "OR (status <> 'certified' AND failure_stage IS NOT NULL "
            "AND failure_code IS NOT NULL))",
            name="coding_certifications_failure_shape",
        ),
        CheckConstraint(
            "transcript_object_key IS NULL "
            "OR transcript_object_key ~ '^sha256/[0-9a-f]{64}$'",
            name="coding_certifications_transcript_key_check",
        ),
        CheckConstraint(
            "frozen_submission_object_key IS NULL "
            "OR frozen_submission_object_key ~ '^sha256/[0-9a-f]{64}$'",
            name="coding_certifications_frozen_key_check",
        ),
        CheckConstraint(
            "status <> 'certified' OR (transcript_object_key IS NOT NULL "
            "AND frozen_submission_object_key IS NOT NULL)",
            name="coding_certifications_certified_artifacts",
        ),
        Index(
            "coding_certifications_agent_created_idx",
            "agent_id",
            "created_at",
        ),
        Index(
            "coding_certifications_active_idx",
            "agent_id",
            "expires_at",
            "validator_hotkey",
            postgresql_where=text("status = 'certified'"),
        ),
    )


class CoreQualificationPolicyRevision(Base):
    """Append-only, benchmark-scoped shadow core qualification policy."""

    __tablename__ = "core_qualification_policy_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    enter_composite: Mapped[float] = mapped_column(Float, nullable=False)
    enter_tool_mean: Mapped[float] = mapped_column(Float, nullable=False)
    enter_memory_mean: Mapped[float] = mapped_column(Float, nullable=False)
    exit_composite: Mapped[float] = mapped_column(Float, nullable=False)
    exit_tool_mean: Mapped[float] = mapped_column(Float, nullable=False)
    exit_memory_mean: Mapped[float] = mapped_column(Float, nullable=False)
    enter_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_observations: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "bench_version >= 7 AND parent_revision >= 0",
            name="core_qualification_policy_identity_check",
        ),
        CheckConstraint(
            "enter_composite BETWEEN 0 AND 1 "
            "AND enter_tool_mean BETWEEN 0 AND 1 "
            "AND enter_memory_mean BETWEEN 0 AND 1 "
            "AND exit_composite BETWEEN 0 AND 1 "
            "AND exit_tool_mean BETWEEN 0 AND 1 "
            "AND exit_memory_mean BETWEEN 0 AND 1",
            name="core_qualification_policy_score_range_check",
        ),
        CheckConstraint(
            "exit_composite <= enter_composite "
            "AND exit_tool_mean <= enter_tool_mean "
            "AND exit_memory_mean <= enter_memory_mean",
            name="core_qualification_policy_hysteresis_check",
        ),
        CheckConstraint(
            "enter_observations BETWEEN 1 AND 20 "
            "AND exit_observations BETWEEN 1 AND 20",
            name="core_qualification_policy_streak_check",
        ),
        CheckConstraint(
            "weight_eligible = false",
            name="core_qualification_policy_weight_ineligible",
        ),
        CheckConstraint(
            "checksum ~ '^[0-9a-f]{64}$'",
            name="core_qualification_policy_checksum_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="core_qualification_policy_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="core_qualification_policy_actor_check",
        ),
        UniqueConstraint(
            "bench_version",
            "parent_revision",
            name="core_qualification_policy_parent_key",
        ),
        UniqueConstraint(
            "revision",
            "bench_version",
            "checksum",
            name="core_qualification_policy_binding_key",
        ),
        Index(
            "core_qualification_policy_bench_revision_idx",
            "bench_version",
            "revision",
            unique=True,
        ),
    )


class CoreQualificationObservation(Base):
    """One immutable score-snapshot transition under a shadow policy."""

    __tablename__ = "core_qualification_observations"

    sequence: Mapped[int] = mapped_column(
        BigInteger,
        Identity(),
        nullable=False,
        unique=True,
    )
    observation_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    screened_image_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    score_evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    full_size: Mapped[bool] = mapped_column(Boolean, nullable=False)
    complete_wave: Mapped[bool] = mapped_column(Boolean, nullable=False)
    score_evidence: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    median_composite: Mapped[float] = mapped_column(Float, nullable=False)
    median_tool_mean: Mapped[float] = mapped_column(Float, nullable=False)
    median_memory_mean: Mapped[float] = mapped_column(Float, nullable=False)
    entry_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    retention_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    qualified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enter_streak: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_streak: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="core_qualification_observations_agent_fkey",
        ),
        ForeignKeyConstraint(
            ["policy_revision", "bench_version", "policy_checksum"],
            [
                "core_qualification_policy_revisions.revision",
                "core_qualification_policy_revisions.bench_version",
                "core_qualification_policy_revisions.checksum",
            ],
            ondelete="RESTRICT",
            name="core_qualification_observations_policy_fkey",
        ),
        CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND policy_checksum ~ '^[0-9a-f]{64}$' "
            "AND score_evidence_sha256 ~ '^[0-9a-f]{64}$'",
            name="core_qualification_observations_hashes_check",
        ),
        CheckConstraint(
            "bench_version >= 7 AND score_count >= 3",
            name="core_qualification_observations_score_count_check",
        ),
        CheckConstraint(
            "median_composite BETWEEN 0 AND 1 "
            "AND median_tool_mean BETWEEN 0 AND 1 "
            "AND median_memory_mean BETWEEN 0 AND 1",
            name="core_qualification_observations_score_range_check",
        ),
        CheckConstraint(
            "enter_streak BETWEEN 0 AND 20 AND exit_streak BETWEEN 0 AND 20",
            name="core_qualification_observations_streak_check",
        ),
        CheckConstraint(
            "decision IN ('partial_wave', 'below_entry', 'pending_entry', 'entered', "
            "'held', 'pending_exit', 'exited')",
            name="core_qualification_observations_decision_check",
        ),
        CheckConstraint(
            "(complete_wave = false AND decision = 'partial_wave') OR "
            "(complete_wave = true AND decision <> 'partial_wave')",
            name="core_qualification_observations_wave_shape_check",
        ),
        CheckConstraint(
            "decision = 'partial_wave' OR "
            "(qualified = true AND decision IN ('entered', 'held', 'pending_exit')) "
            "OR (qualified = false AND decision IN "
            "('below_entry', 'pending_entry', 'exited'))",
            name="core_qualification_observations_decision_state_check",
        ),
        CheckConstraint(
            "(source = 'score_commit' AND actor IS NULL AND reason IS NULL) OR "
            "(source = 'admin_refresh' AND length(trim(actor)) BETWEEN 1 AND 120 "
            "AND length(trim(reason)) >= 8)",
            name="core_qualification_observations_source_check",
        ),
        CheckConstraint(
            "weight_eligible = false",
            name="core_qualification_observations_weight_ineligible",
        ),
        UniqueConstraint(
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "bench_version",
            "policy_revision",
            "score_evidence_sha256",
            name="core_qualification_observations_evidence_key",
        ),
        Index(
            "core_qualification_observations_agent_bench_sequence_idx",
            "agent_id",
            "bench_version",
            "sequence",
        ),
    )


class CodingCatalogRelease(Base):
    """One immutable curator-signed private catalog commitment."""

    __tablename__ = "coding_catalog_releases"

    release_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    corpus_release_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    coding_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    catalog_merkle_root: Mapped[str] = mapped_column(Text, nullable=False)
    selection_derivation_id: Mapped[str] = mapped_column(Text, nullable=False)
    selection_chain_genesis_hash: Mapped[str] = mapped_column(Text, nullable=False)
    grader_contract_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    inference_grant_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    task_version_count: Mapped[int] = mapped_column(Integer, nullable=False)
    curator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    committed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    commitment_sha256: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    commitment: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "release_row_id",
            "corpus_release_id",
            name="coding_catalog_releases_row_corpus_key",
        ),
        UniqueConstraint(
            "release_row_id",
            "commitment_sha256",
            name="coding_catalog_releases_row_commitment_key",
        ),
        CheckConstraint(
            "coding_contract_version = 1 AND weight_eligible = false "
            "AND task_version_count BETWEEN 1 AND 1000000",
            name="coding_catalog_releases_contract_check",
        ),
        CheckConstraint(
            "catalog_merkle_root ~ '^[0-9a-f]{64}$' "
            "AND grader_contract_sha256 ~ '^[0-9a-f]{64}$' "
            "AND inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND commitment_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_catalog_releases_hashes_check",
        ),
        CheckConstraint(
            "selection_chain_genesis_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_catalog_releases_genesis_check",
        ),
        CheckConstraint(
            "curator_hotkey ~ '^[1-9A-HJ-NP-Za-km-z]{47,48}$' "
            "AND signature ~ '^[0-9a-f]{128}$'",
            name="coding_catalog_releases_signature_check",
        ),
        CheckConstraint(
            "octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND octet_length(selection_derivation_id) BETWEEN 1 AND 128 "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]' "
            "AND selection_derivation_id !~ '[[:space:][:cntrl:]]'",
            name="coding_catalog_releases_identifiers_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8 AND length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_catalog_releases_audit_check",
        ),
        Index("coding_catalog_releases_created_idx", "created_at"),
    )


class CodingCatalogRetirement(Base):
    """One append-only terminal retirement for a catalog commitment."""

    __tablename__ = "coding_catalog_retirements"

    release_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    expected_commitment_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    retired_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["release_row_id", "expected_commitment_sha256"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.commitment_sha256",
            ],
            ondelete="RESTRICT",
            name="coding_catalog_retirements_release_fkey",
        ),
        CheckConstraint(
            "expected_commitment_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_catalog_retirements_commitment_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8 AND length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_catalog_retirements_audit_check",
        ),
    )


class CodingCatalogExposure(Base):
    """Irreversible private task-version consumption before lease issuance."""

    __tablename__ = "coding_catalog_exposures"

    exposure_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    release_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    corpus_release_id: Mapped[str] = mapped_column(Text, nullable=False)
    run_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    run_task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_index: Mapped[int] = mapped_column(Integer, nullable=False)
    task_version_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_commitment_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    selection_proof_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_membership_proof_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    visible_bundle_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    base_tree_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    memory_bundle_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    environment_image_digest: Mapped[str] = mapped_column(Text, nullable=False)
    resource_profile_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    grader_bundle_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    grader_image_digest: Mapped[str] = mapped_column(Text, nullable=False)
    test_manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    grader_plan_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exposed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["release_row_id", "corpus_release_id"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.corpus_release_id",
            ],
            ondelete="RESTRICT",
            name="coding_catalog_exposures_release_fkey",
        ),
        ForeignKeyConstraint(
            ["run_row_id", "corpus_release_id", "run_task_count"],
            [
                "coding_shadow_runs.run_row_id",
                "coding_shadow_runs.corpus_release_id",
                "coding_shadow_runs.task_count",
            ],
            ondelete="RESTRICT",
            name="coding_catalog_exposures_run_fkey",
        ),
        UniqueConstraint(
            "task_version_id",
            name="coding_catalog_exposures_task_version_key",
        ),
        UniqueConstraint(
            "run_row_id",
            "manifest_index",
            name="coding_catalog_exposures_run_index_key",
        ),
        CheckConstraint(
            "run_task_count BETWEEN 1 AND 100 "
            "AND manifest_index >= 0 AND manifest_index < run_task_count",
            name="coding_catalog_exposures_index_check",
        ),
        CheckConstraint(
            "octet_length(task_version_id) BETWEEN 1 AND 256 "
            "AND task_version_id !~ '[[:space:][:cntrl:]]'",
            name="coding_catalog_exposures_task_version_check",
        ),
        CheckConstraint(
            "task_commitment_sha256 ~ '^[0-9a-f]{64}$' "
            "AND selection_proof_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_membership_proof_sha256 ~ '^[0-9a-f]{64}$' "
            "AND visible_bundle_sha256 ~ '^[0-9a-f]{64}$' "
            "AND base_tree_sha256 ~ '^[0-9a-f]{64}$' "
            "AND memory_bundle_sha256 ~ '^[0-9a-f]{64}$' "
            "AND resource_profile_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grader_bundle_sha256 ~ '^[0-9a-f]{64}$' "
            "AND test_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grader_plan_sha256 ~ '^[0-9a-f]{64}$' "
            "AND environment_image_digest ~ '^sha256:[0-9a-f]{64}$' "
            "AND grader_image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="coding_catalog_exposures_digests_check",
        ),
        CheckConstraint(
            "weight_eligible = false",
            name="coding_catalog_exposures_weight_ineligible",
        ),
        Index("coding_catalog_exposures_run_idx", "run_row_id", "manifest_index"),
    )


class CodingSelectionAssignmentRow(Base):
    """One immutable future-height assignment created before task selection."""

    __tablename__ = "coding_selection_assignments"

    assignment_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    assignment_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    release_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    screened_image_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    coding_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    coding_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    corpus_release_id: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_commitment_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    anchor_block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    anchor_block_hash: Mapped[str] = mapped_column(Text, nullable=False)
    selection_delay_blocks: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    core_qualification_observation_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), nullable=False
    )
    certification_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), nullable=False
    )
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    assignment: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="coding_selection_assignments_agent_fkey",
        ),
        ForeignKeyConstraint(
            ["release_row_id", "corpus_release_id"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.corpus_release_id",
            ],
            ondelete="RESTRICT",
            name="coding_selection_assignments_release_fkey",
        ),
        ForeignKeyConstraint(
            ["release_row_id", "catalog_commitment_sha256"],
            [
                "coding_catalog_releases.release_row_id",
                "coding_catalog_releases.commitment_sha256",
            ],
            ondelete="RESTRICT",
            name="coding_selection_assignments_commitment_fkey",
        ),
        ForeignKeyConstraint(
            ["core_qualification_observation_id"],
            ["core_qualification_observations.observation_id"],
            ondelete="RESTRICT",
            name="coding_selection_assignments_core_observation_fkey",
        ),
        ForeignKeyConstraint(
            ["certification_row_id"],
            ["coding_capability_certifications.certification_row_id"],
            ondelete="RESTRICT",
            name="coding_selection_assignments_certification_fkey",
        ),
        UniqueConstraint(
            "assignment_sha256",
            name="coding_selection_assignments_assignment_sha256_key",
        ),
        UniqueConstraint(
            "agent_id",
            "coding_contract_version",
            "coding_run_id",
            name="coding_selection_assignments_identity_key",
        ),
        UniqueConstraint(
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "coding_contract_version",
            name="coding_selection_assignments_artifact_key",
        ),
        UniqueConstraint(
            "assignment_row_id",
            "assignment_sha256",
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "bench_version",
            "coding_contract_version",
            "coding_run_id",
            "corpus_release_id",
            "selection_block_number",
            "task_count",
            name="coding_selection_assignments_run_authority_key",
        ),
        CheckConstraint(
            "assignment_sha256 ~ '^[0-9a-f]{64}$' "
            "AND artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_commitment_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_selection_assignments_hashes_check",
        ),
        CheckConstraint(
            "anchor_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_selection_assignments_anchor_hash_check",
        ),
        CheckConstraint(
            "bench_version >= 7 AND coding_contract_version = 1 "
            "AND anchor_block_number > 0 "
            "AND selection_delay_blocks BETWEEN 1 AND 10000 "
            "AND selection_block_number = anchor_block_number + selection_delay_blocks "
            "AND task_count = 1 AND weight_eligible = false",
            name="coding_selection_assignments_contract_check",
        ),
        CheckConstraint(
            "octet_length(coding_run_id) BETWEEN 1 AND 256 "
            "AND octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND coding_run_id !~ '[[:space:][:cntrl:]]' "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]'",
            name="coding_selection_assignments_identifiers_check",
        ),
        CheckConstraint(
            "assigned_at = created_at",
            name="coding_selection_assignments_time_check",
        ),
        Index(
            "coding_selection_assignments_agent_created_idx",
            "agent_id",
            "created_at",
        ),
        Index(
            "coding_selection_assignments_height_idx",
            "selection_block_number",
            "created_at",
        ),
    )


class CodingShadowRun(Base):
    """One immutable shared run authority for the shadow coding pipeline."""

    __tablename__ = "coding_shadow_runs"

    run_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    screened_image_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    coding_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    coding_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    corpus_release_id: Mapped[str] = mapped_column(Text, nullable=False)
    catalog_merkle_root: Mapped[str] = mapped_column(Text, nullable=False)
    selection_derivation_id: Mapped[str] = mapped_column(Text, nullable=False)
    selection_chain_genesis_hash: Mapped[str] = mapped_column(Text, nullable=False)
    selection_block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    selection_block_hash: Mapped[str] = mapped_column(Text, nullable=False)
    inference_grant_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    grader_contract_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    task_set_id: Mapped[str] = mapped_column(Text, nullable=False)
    task_set_manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    run_manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    core_qualification_observation_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), nullable=False
    )
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="coding_shadow_runs_agent_fkey",
        ),
        ForeignKeyConstraint(
            ["core_qualification_observation_id"],
            ["core_qualification_observations.observation_id"],
            ondelete="CASCADE",
            name="coding_shadow_runs_core_observation_fkey",
        ),
        UniqueConstraint(
            "agent_id",
            "coding_contract_version",
            "coding_run_id",
            name="coding_shadow_runs_identity_key",
        ),
        UniqueConstraint(
            "run_row_id",
            "task_count",
            name="coding_shadow_runs_run_task_count_key",
        ),
        UniqueConstraint(
            "run_row_id",
            "corpus_release_id",
            "task_count",
            name="coding_shadow_runs_run_corpus_task_count_key",
        ),
        UniqueConstraint(
            "run_row_id",
            "agent_id",
            "artifact_sha256",
            "screened_image_sha256",
            "bench_version",
            "coding_contract_version",
            "coding_run_id",
            "corpus_release_id",
            "selection_block_number",
            "selection_block_hash",
            "task_count",
            name="coding_shadow_runs_issuance_authority_key",
        ),
        CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND catalog_merkle_root ~ '^[0-9a-f]{64}$' "
            "AND inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND grader_contract_sha256 ~ '^[0-9a-f]{64}$' "
            "AND task_set_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND run_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_shadow_runs_hashes_check",
        ),
        CheckConstraint(
            "selection_chain_genesis_hash ~ '^0x[0-9a-f]{64}$' "
            "AND selection_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_shadow_runs_block_hashes_check",
        ),
        CheckConstraint(
            "bench_version >= 7 AND coding_contract_version = 1 "
            "AND selection_block_number > 0 AND task_count BETWEEN 1 AND 100",
            name="coding_shadow_runs_bounds_check",
        ),
        CheckConstraint(
            "octet_length(coding_run_id) BETWEEN 1 AND 256 "
            "AND octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND octet_length(selection_derivation_id) BETWEEN 1 AND 128 "
            "AND octet_length(task_set_id) BETWEEN 1 AND 256 "
            "AND coding_run_id !~ '[[:space:][:cntrl:]]' "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]' "
            "AND selection_derivation_id !~ '[[:space:][:cntrl:]]' "
            "AND task_set_id !~ '[[:space:][:cntrl:]]'",
            name="coding_shadow_runs_identifiers_check",
        ),
        CheckConstraint(
            "weight_eligible = false",
            name="coding_shadow_runs_weight_ineligible",
        ),
        Index(
            "coding_shadow_runs_agent_created_idx",
            "agent_id",
            "created_at",
        ),
    )


class CodingShadowRunIssuance(Base):
    """One immutable finalized assignment-to-run issuance bridge."""

    __tablename__ = "coding_shadow_run_issuances"

    assignment_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True
    )
    run_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    assignment_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    screened_image_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    coding_contract_version: Mapped[int] = mapped_column(Integer, nullable=False)
    coding_run_id: Mapped[str] = mapped_column(Text, nullable=False)
    corpus_release_id: Mapped[str] = mapped_column(Text, nullable=False)
    selection_block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    selection_block_hash: Mapped[str] = mapped_column(Text, nullable=False)
    selection_candidate_probe: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_catalog_index: Mapped[int] = mapped_column(Integer, nullable=False)
    selection_proof_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    selection_block_timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )

    __table_args__ = (
        UniqueConstraint(
            "run_row_id",
            name="coding_shadow_run_issuances_run_key",
        ),
        ForeignKeyConstraint(
            [
                "assignment_row_id",
                "assignment_sha256",
                "agent_id",
                "artifact_sha256",
                "screened_image_sha256",
                "bench_version",
                "coding_contract_version",
                "coding_run_id",
                "corpus_release_id",
                "selection_block_number",
                "task_count",
            ],
            [
                "coding_selection_assignments.assignment_row_id",
                "coding_selection_assignments.assignment_sha256",
                "coding_selection_assignments.agent_id",
                "coding_selection_assignments.artifact_sha256",
                "coding_selection_assignments.screened_image_sha256",
                "coding_selection_assignments.bench_version",
                "coding_selection_assignments.coding_contract_version",
                "coding_selection_assignments.coding_run_id",
                "coding_selection_assignments.corpus_release_id",
                "coding_selection_assignments.selection_block_number",
                "coding_selection_assignments.task_count",
            ],
            ondelete="RESTRICT",
            name="coding_shadow_run_issuances_assignment_fkey",
        ),
        ForeignKeyConstraint(
            [
                "run_row_id",
                "agent_id",
                "artifact_sha256",
                "screened_image_sha256",
                "bench_version",
                "coding_contract_version",
                "coding_run_id",
                "corpus_release_id",
                "selection_block_number",
                "selection_block_hash",
                "task_count",
            ],
            [
                "coding_shadow_runs.run_row_id",
                "coding_shadow_runs.agent_id",
                "coding_shadow_runs.artifact_sha256",
                "coding_shadow_runs.screened_image_sha256",
                "coding_shadow_runs.bench_version",
                "coding_shadow_runs.coding_contract_version",
                "coding_shadow_runs.coding_run_id",
                "coding_shadow_runs.corpus_release_id",
                "coding_shadow_runs.selection_block_number",
                "coding_shadow_runs.selection_block_hash",
                "coding_shadow_runs.task_count",
            ],
            ondelete="RESTRICT",
            name="coding_shadow_run_issuances_run_fkey",
        ),
        CheckConstraint(
            "assignment_sha256 ~ '^[0-9a-f]{64}$' "
            "AND artifact_sha256 ~ '^[0-9a-f]{64}$' "
            "AND screened_image_sha256 ~ '^[0-9a-f]{64}$' "
            "AND selection_proof_sha256 ~ '^[0-9a-f]{64}$'",
            name="coding_shadow_run_issuances_hashes_check",
        ),
        CheckConstraint(
            "selection_block_hash ~ '^0x[0-9a-f]{64}$'",
            name="coding_shadow_run_issuances_block_hash_check",
        ),
        CheckConstraint(
            "bench_version >= 7 AND coding_contract_version = 1 "
            "AND selection_block_number > 0 AND task_count = 1 "
            "AND selection_candidate_probe BETWEEN 0 AND 999999 "
            "AND selection_catalog_index BETWEEN 0 AND 999999 "
            "AND weight_eligible = false "
            "AND issued_at >= selection_block_timestamp - interval '5 seconds'",
            name="coding_shadow_run_issuances_contract_check",
        ),
        CheckConstraint(
            "octet_length(coding_run_id) BETWEEN 1 AND 256 "
            "AND octet_length(corpus_release_id) BETWEEN 1 AND 256 "
            "AND coding_run_id !~ '[[:space:][:cntrl:]]' "
            "AND corpus_release_id !~ '[[:space:][:cntrl:]]'",
            name="coding_shadow_run_issuances_identifiers_check",
        ),
        Index(
            "coding_shadow_run_issuances_agent_issued_idx",
            "agent_id",
            "issued_at",
        ),
    )


class CodingShadowTicket(Base):
    """Validator-specific lease over one immutable shared coding run."""

    __tablename__ = "coding_shadow_tickets"

    ticket_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    run_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    certification_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    claim_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    claim_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_acquired_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    claim_heartbeat_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    claim_started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["run_row_id", "task_count"],
            ["coding_shadow_runs.run_row_id", "coding_shadow_runs.task_count"],
            ondelete="CASCADE",
            name="coding_shadow_tickets_run_fkey",
        ),
        ForeignKeyConstraint(
            ["certification_row_id"],
            ["coding_capability_certifications.certification_row_id"],
            ondelete="CASCADE",
            name="coding_shadow_tickets_certification_fkey",
        ),
        UniqueConstraint(
            "run_row_id",
            "validator_hotkey",
            name="coding_shadow_tickets_run_validator_key",
        ),
        UniqueConstraint(
            "ticket_id",
            "run_row_id",
            "task_count",
            name="coding_shadow_tickets_ticket_run_task_count_key",
        ),
        CheckConstraint(
            "task_count BETWEEN 1 AND 100",
            name="coding_shadow_tickets_task_count_check",
        ),
        CheckConstraint(
            "validator_hotkey ~ '^[1-9A-HJ-NP-Za-km-z]{47,48}$'",
            name="coding_shadow_tickets_validator_check",
        ),
        CheckConstraint(
            "deadline > issued_at AND deadline <= issued_at + interval '2 hours'",
            name="coding_shadow_tickets_deadline_check",
        ),
        CheckConstraint(
            "(claim_generation = 0 AND claim_instance_id IS NULL "
            "AND claim_acquired_at IS NULL AND claim_heartbeat_at IS NULL "
            "AND claim_expires_at IS NULL AND claim_started_at IS NULL) OR "
            "(claim_generation BETWEEN 1 AND 2147483647 AND "
            "((claim_instance_id IS NULL AND claim_acquired_at IS NULL "
            "AND claim_heartbeat_at IS NULL AND claim_expires_at IS NULL "
            "AND claim_started_at IS NULL) OR "
            "(claim_instance_id IS NOT NULL AND "
            "octet_length(claim_instance_id) BETWEEN 1 AND 128 "
            "AND claim_instance_id !~ '[[:space:][:cntrl:]]' "
            "AND claim_acquired_at IS NOT NULL AND claim_heartbeat_at IS NOT NULL "
            "AND claim_expires_at IS NOT NULL "
            "AND claim_heartbeat_at >= claim_acquired_at "
            "AND claim_expires_at > claim_heartbeat_at "
            "AND claim_expires_at <= deadline "
            "AND (claim_started_at IS NULL OR "
            "(claim_started_at >= claim_acquired_at "
            "AND claim_started_at <= claim_heartbeat_at "
            "AND claim_started_at < deadline)))))",
            name="coding_shadow_tickets_claim_check",
        ),
        Index(
            "coding_shadow_tickets_validator_deadline_idx",
            "validator_hotkey",
            "deadline",
        ),
        Index(
            "coding_shadow_tickets_claim_instance_idx",
            "validator_hotkey",
            "claim_instance_id",
            "claim_expires_at",
        ),
        Index(
            "coding_shadow_tickets_claim_instance_key",
            "validator_hotkey",
            "claim_instance_id",
            unique=True,
            postgresql_where=text("claim_instance_id IS NOT NULL"),
        ),
    )


class CodingInferenceGrant(Base):
    """One locked Luna capability bound to one shadow coding ticket."""

    __tablename__ = "coding_inference_grants"

    grant_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    ticket_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    run_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    case_id: Mapped[str] = mapped_column(Text, nullable=False)
    profile_capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    inference_grant_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider_api: Mapped[str] = mapped_column(Text, nullable=False)
    provider_route: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_route_profile: Mapped[str] = mapped_column(Text, nullable=False)
    provider_account_guardrail: Mapped[str] = mapped_column(Text, nullable=False)
    provider_pipeline_policy: Mapped[str] = mapped_column(Text, nullable=False)
    provider_cache_policy: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning_effort: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    bearer_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    revoke_bearer_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_token_budget: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completion_token_budget: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_budget_usd_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cost_usd_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    active_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["ticket_id", "run_row_id", "task_count"],
            [
                "coding_shadow_tickets.ticket_id",
                "coding_shadow_tickets.run_row_id",
                "coding_shadow_tickets.task_count",
            ],
            ondelete="CASCADE",
            name="coding_inference_grants_ticket_fkey",
        ),
        UniqueConstraint(
            "ticket_id",
            name="coding_inference_grants_ticket_key",
        ),
        UniqueConstraint(
            "grant_id",
            "ticket_id",
            name="coding_inference_grants_grant_ticket_key",
        ),
        CheckConstraint(
            "task_count = 1 AND generation BETWEEN 0 AND 2147483647 "
            "AND request_budget BETWEEN 1 AND 256 "
            "AND prompt_token_budget BETWEEN 1 AND 2000000 "
            "AND completion_token_budget BETWEEN 1 AND 250000 "
            "AND cost_budget_usd_micros BETWEEN 1 AND 100000000",
            name="coding_inference_grants_authority_bounds_check",
        ),
        CheckConstraint(
            "request_count BETWEEN 0 AND request_budget "
            "AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 "
            "AND cost_usd_micros >= 0 "
            "AND active_requests BETWEEN 0 AND 1",
            name="coding_inference_grants_accounting_check",
        ),
        CheckConstraint(
            "inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (bearer_digest IS NULL OR bearer_digest ~ '^[0-9a-f]{64}$') "
            "AND (revoke_bearer_digest IS NULL OR "
            "revoke_bearer_digest ~ '^[0-9a-f]{64}$') "
            "AND (broker_public_key IS NULL OR "
            "broker_public_key ~ '^[A-Za-z0-9_-]{43}$')",
            name="coding_inference_grants_crypto_check",
        ),
        CheckConstraint(
            "model = 'openai/gpt-5.6-luna' "
            "AND provider_api = 'openrouter' "
            "AND provider_account_guardrail = 'openrouter_private_account_v1' "
            "AND provider_pipeline_policy = 'no_plugins_no_transforms_v1' "
            "AND provider_cache_policy = 'disabled_v1' "
            "AND reasoning_effort = 'medium'",
            name="coding_inference_grants_locked_route_check",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'exhausted') "
            "AND ((status = 'pending' AND generation = 0 "
            "AND bearer_digest IS NULL AND revoke_bearer_digest IS NULL "
            "AND broker_public_key IS NULL) "
            "OR (status = 'active' AND generation > 0 "
            "AND bearer_digest IS NOT NULL AND broker_public_key IS NOT NULL) "
            "OR (status IN ('revoked', 'exhausted') "
            "AND bearer_digest IS NULL AND broker_public_key IS NULL)) "
            "AND ((status = 'revoked') = (revoked_at IS NOT NULL))",
            name="coding_inference_grants_state_check",
        ),
        CheckConstraint(
            "octet_length(case_id) BETWEEN 1 AND 256 "
            "AND octet_length(profile_capability_id) BETWEEN 1 AND 256 "
            "AND octet_length(provider_route) BETWEEN 1 AND 128 "
            "AND octet_length(receipt_provider) BETWEEN 1 AND 128 "
            "AND octet_length(provider_route_profile) BETWEEN 1 AND 128 "
            "AND case_id !~ '[[:space:][:cntrl:]]' "
            "AND profile_capability_id !~ '[[:space:][:cntrl:]]'",
            name="coding_inference_grants_identifiers_check",
        ),
        CheckConstraint(
            "expires_at > created_at AND weight_eligible = false",
            name="coding_inference_grants_shadow_check",
        ),
        Index(
            "coding_inference_grants_validator_expiry_idx",
            "validator_hotkey",
            "expires_at",
            postgresql_where=text("status IN ('pending', 'active')"),
        ),
    )


class CodingInferenceRequest(Base):
    """One durable request reservation and its trusted provider settlement."""

    __tablename__ = "coding_inference_requests"

    request_row_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    grant_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    ticket_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    request_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    case_id: Mapped[str] = mapped_column(Text, nullable=False)
    profile_capability_id: Mapped[str] = mapped_column(Text, nullable=False)
    inference_grant_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    locked_request_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider_settlement_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_generation_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_settlement_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    unsettled_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["grant_id", "ticket_id"],
            [
                "coding_inference_grants.grant_id",
                "coding_inference_grants.ticket_id",
            ],
            ondelete="CASCADE",
            name="coding_inference_requests_grant_fkey",
        ),
        UniqueConstraint(
            "grant_id",
            "sequence",
            name="coding_inference_requests_grant_sequence_key",
        ),
        UniqueConstraint(
            "grant_id",
            "request_sequence",
            "attempt",
            name="coding_inference_requests_request_attempt_key",
        ),
        UniqueConstraint(
            "grant_id",
            "request_id",
            "attempt",
            name="coding_inference_requests_request_id_attempt_key",
        ),
        UniqueConstraint(
            "provider_settlement_sha256",
            name="coding_inference_requests_settlement_key",
        ),
        UniqueConstraint(
            "provider_generation_id",
            name="coding_inference_requests_provider_generation_key",
        ),
        CheckConstraint(
            "generation BETWEEN 1 AND 2147483647 "
            "AND sequence BETWEEN 1 AND 1100 "
            "AND request_sequence BETWEEN 1 AND 256 "
            "AND attempt BETWEEN 1 AND 3",
            name="coding_inference_requests_order_check",
        ),
        CheckConstraint(
            "inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
            "AND locked_request_sha256 ~ '^[0-9a-f]{64}$' "
            "AND (provider_settlement_sha256 IS NULL OR "
            "provider_settlement_sha256 ~ '^[0-9a-f]{64}$') "
            "AND (provider_generation_id IS NULL OR "
            "(octet_length(provider_generation_id) BETWEEN 1 AND 256 "
            "AND provider_generation_id !~ '[[:space:][:cntrl:]]'))",
            name="coding_inference_requests_digests_check",
        ),
        CheckConstraint(
            "octet_length(case_id) BETWEEN 1 AND 256 "
            "AND octet_length(profile_capability_id) BETWEEN 1 AND 256 "
            "AND case_id !~ '[[:space:][:cntrl:]]' "
            "AND profile_capability_id !~ '[[:space:][:cntrl:]]' "
            "AND (provider_settlement_json IS NULL OR "
            "octet_length(provider_settlement_json) BETWEEN 1 AND 65536)",
            name="coding_inference_requests_bounds_check",
        ),
        CheckConstraint(
            "status IN ('started', 'receipt_free_retry', 'complete', "
            "'provider_failure', 'unsettled') "
            "AND ((status = 'started' "
            "AND provider_settlement_sha256 IS NULL "
            "AND provider_generation_id IS NULL "
            "AND provider_settlement_json IS NULL "
            "AND unsettled_reason IS NULL AND settled_at IS NULL) "
            "OR (status IN ('receipt_free_retry', 'complete', "
            "'provider_failure') "
            "AND provider_settlement_sha256 IS NOT NULL "
            "AND provider_settlement_json IS NOT NULL "
            "AND unsettled_reason IS NULL AND settled_at IS NOT NULL) "
            "OR (status = 'unsettled' "
            "AND provider_settlement_sha256 IS NULL "
            "AND provider_generation_id IS NULL "
            "AND provider_settlement_json IS NULL "
            "AND unsettled_reason IN ('provider_settlement_unavailable', "
            "'provider_response_lost', 'relay_infrastructure', "
            "'invalid_provider_settlement') "
            "AND settled_at IS NOT NULL)) "
            "AND (settled_at IS NULL OR settled_at >= started_at) "
            "AND weight_eligible = false",
            name="coding_inference_requests_state_check",
        ),
        Index(
            "coding_inference_requests_grant_status_idx",
            "grant_id",
            "status",
            "sequence",
        ),
    )


class CodingShadowAuthoringFreeze(Base):
    """One immutable validator-signed freeze transition for a shadow ticket."""

    __tablename__ = "coding_shadow_authoring_freezes"

    freeze_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    ticket_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    run_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    authoring_evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    authoring_event_root: Mapped[str] = mapped_column(Text, nullable=False)
    authoring_transcript_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    authoring_transcript_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    authoring_transcript_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authoring_event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_patch_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_submission_object_key: Mapped[str] = mapped_column(Text, nullable=False)
    changed_path_root: Mapped[str] = mapped_column(Text, nullable=False)
    final_tree_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    changed_path_count: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    protected_paths_intact: Mapped[bool] = mapped_column(Boolean, nullable=False)
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["ticket_id", "run_row_id", "task_count"],
            [
                "coding_shadow_tickets.ticket_id",
                "coding_shadow_tickets.run_row_id",
                "coding_shadow_tickets.task_count",
            ],
            ondelete="CASCADE",
            name="coding_shadow_authoring_freezes_ticket_fkey",
        ),
        UniqueConstraint(
            "ticket_id",
            name="coding_shadow_authoring_freezes_ticket_key",
        ),
        CheckConstraint(
            "authoring_evidence_sha256 ~ '^[0-9a-f]{64}$' "
            "AND authoring_event_root ~ '^[0-9a-f]{64}$' "
            "AND authoring_transcript_sha256 ~ '^[0-9a-f]{64}$' "
            "AND frozen_patch_sha256 ~ '^[0-9a-f]{64}$' "
            "AND changed_path_root ~ '^[0-9a-f]{64}$' "
            "AND final_tree_sha256 ~ '^[0-9a-f]{64}$' "
            "AND signature ~ '^[0-9a-f]{128}$'",
            name="coding_shadow_authoring_freezes_integrity_check",
        ),
        CheckConstraint(
            "authoring_transcript_object_key = "
            "'sha256/' || authoring_transcript_sha256 "
            "AND frozen_submission_object_key = 'sha256/' || frozen_patch_sha256",
            name="coding_shadow_authoring_freezes_object_keys_check",
        ),
        CheckConstraint(
            "task_count = 1 AND changed_path_count BETWEEN 0 AND 10000 "
            "AND changed_bytes BETWEEN 0 AND 1073741824 "
            "AND authoring_transcript_bytes BETWEEN 0 AND 536870912 "
            "AND authoring_event_count BETWEEN 0 AND 1000 "
            "AND (authoring_transcript_bytes = 0) = (authoring_event_count = 0)",
            name="coding_shadow_authoring_freezes_bounds_check",
        ),
        CheckConstraint(
            "weight_eligible = false",
            name="coding_shadow_authoring_freezes_weight_ineligible",
        ),
        Index(
            "coding_shadow_authoring_freezes_run_created_idx",
            "run_row_id",
            "created_at",
        ),
    )


class CodingShadowResult(Base):
    """One immutable validator-signed repair result for a shadow ticket."""

    __tablename__ = "coding_shadow_results"

    result_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    ticket_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    run_row_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    run_evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    resolved_count: Mapped[int] = mapped_column(Integer, nullable=False)
    repair_failure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    infrastructure_count: Mapped[int] = mapped_column(Integer, nullable=False)
    invalid_count: Mapped[int] = mapped_column(Integer, nullable=False)
    candidate_integrity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    control_plane_integrity_count: Mapped[int] = mapped_column(Integer, nullable=False)
    scoreable_task_count: Mapped[int] = mapped_column(Integer, nullable=False)
    repair_mean_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    weight_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["ticket_id", "run_row_id", "task_count"],
            [
                "coding_shadow_tickets.ticket_id",
                "coding_shadow_tickets.run_row_id",
                "coding_shadow_tickets.task_count",
            ],
            ondelete="CASCADE",
            name="coding_shadow_results_ticket_fkey",
        ),
        UniqueConstraint("ticket_id", name="coding_shadow_results_ticket_key"),
        CheckConstraint(
            "run_evidence_sha256 ~ '^[0-9a-f]{64}$' AND signature ~ '^[0-9a-f]{128}$'",
            name="coding_shadow_results_integrity_check",
        ),
        CheckConstraint(
            "task_count BETWEEN 1 AND 100 "
            "AND resolved_count >= 0 AND repair_failure_count >= 0 "
            "AND infrastructure_count >= 0 AND invalid_count >= 0 "
            "AND candidate_integrity_count >= 0 "
            "AND control_plane_integrity_count >= 0 "
            "AND scoreable_task_count >= 0 "
            "AND repair_mean_micros BETWEEN 0 AND 1000000",
            name="coding_shadow_results_bounds_check",
        ),
        CheckConstraint(
            "resolved_count + repair_failure_count + infrastructure_count + "
            "invalid_count + candidate_integrity_count + "
            "control_plane_integrity_count = task_count "
            "AND scoreable_task_count = resolved_count + repair_failure_count + "
            "candidate_integrity_count",
            name="coding_shadow_results_counts_check",
        ),
        CheckConstraint(
            "(scoreable_task_count = 0 AND repair_mean_micros = 0) OR "
            "(scoreable_task_count > 0 AND repair_mean_micros = "
            "(resolved_count * 1000000) / scoreable_task_count)",
            name="coding_shadow_results_mean_check",
        ),
        CheckConstraint(
            "weight_eligible = false",
            name="coding_shadow_results_weight_ineligible",
        ),
        Index(
            "coding_shadow_results_run_created_idx",
            "run_row_id",
            "created_at",
        ),
    )


class Score(Base):
    """One validator's DittoBench score for one agent.

    The composite primary key ``(agent_id, validator_hotkey)`` keeps a
    single current score per validator per agent: a validator re-scoring
    the same agent (new ``run_id``) upserts this row rather than appending
    history. ``agent_id`` is a single-column FK to ``agents.agent_id`` with
    ``ON DELETE CASCADE`` because a score is derived data — deleting the
    agent discards its scores.

    Aggregates (``composite`` / ``tool_mean`` / ``memory_mean`` /
    ``median_ms`` / ``n``) are first-class columns so weight computation and
    leaderboards query them directly; ``details`` carries the optional
    per-case breakdown verbatim for audit/dispute. The platform records what
    the validator reports and never recomputes ``composite``.
    """

    __tablename__ = "scores"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """FK to ``agents.agent_id``. PK part 1."""

    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    """SS58 hotkey of the reporting validator. PK part 2."""

    bench_version: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    """Benchmark semantics this signed score and its dataset were produced under.

    Defaults to the floor, not to 2. The old default predated any floor and is
    now guaranteed to violate ``scores_bench_version_floor``, so it could only
    ever have produced a runtime IntegrityError. Every production caller passes
    this explicitly -- ``upsert_score`` requires it -- so the default now serves
    only fixtures that do not care which era they are in.
    """

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    """Scoring-engine run identifier (part of the value the signature is bound to)."""

    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The reporting validator's sr25519 signature over the score payload, hex
    encoded. Persisted so the exposed ledger is self-verifying. Nullable for rows
    written before the ledger migration."""

    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Dataset seed used for the run (anti-overfit reproducibility)."""

    composite: Mapped[float] = mapped_column(Float, nullable=False)
    """Aggregate benchmark score in [0, 1] (not recomputed by the platform)."""

    tool_mean: Mapped[float] = mapped_column(Float, nullable=False)
    """Mean tool accuracy in [0, 1]."""

    memory_mean: Mapped[float] = mapped_column(Float, nullable=False)
    """Mean memory recall in [0, 1]."""

    median_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    """Median per-case latency in milliseconds."""

    n: Mapped[int] = mapped_column(Integer, nullable=False)
    """Number of cases scored."""

    details: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    """Optional per-case breakdown ``{"per_case": [...]}`` for audit."""

    model_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Reader-model calls this run made, off the lease's inference grant.

    Denormalized at submit time from ``inference_grants``. That table is hot
    and short-retention (grants are pruned within days) while scores are
    permanent, so the join is not merely slow to reproduce later -- after
    pruning it is impossible. ``NULL`` means *not measured* (no grant existed
    for the lease), which is distinct from ``0`` meaning *measured, and the
    model was never called*.
    """

    model_prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Prompt tokens the reader model was sent across this run."""

    model_completion_tokens: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    """Completion tokens the reader model produced across this run."""

    generated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    """When the scoring engine produced the report (UTC)."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When this row was first inserted (UTC)."""

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    """When this row was last upserted (UTC)."""

    __table_args__ = (
        PrimaryKeyConstraint(
            "agent_id", "bench_version", "validator_hotkey", name="scores_pkey"
        ),
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="scores_agent_id_fkey",
        ),
        CheckConstraint(
            "composite >= 0 AND composite <= 1",
            name="scores_composite_range_check",
        ),
        CheckConstraint(
            "tool_mean >= 0 AND tool_mean <= 1", name="scores_tool_mean_range_check"
        ),
        CheckConstraint(
            "memory_mean >= 0 AND memory_mean <= 1",
            name="scores_memory_mean_range_check",
        ),
        CheckConstraint("n >= 0", name="scores_n_check"),
        CheckConstraint("median_ms >= 0", name="scores_median_ms_check"),
        CheckConstraint("bench_version > 0", name="scores_bench_version_positive"),
        # The retired-era floor. Declared NOT VALID in the migration: the
        # historical v2-v6 rows stay readable, queryable and byte-identical,
        # while every INSERT and UPDATE from here on must be v7 or later. This
        # is the guarantee that no application bug can score a retired era --
        # see MIN_SCOREABLE_BENCH_VERSION.
        CheckConstraint("bench_version >= 7", name="scores_bench_version_floor"),
        Index("scores_agent_id_idx", "agent_id"),
        # Dashboard/ledger reads select one benchmark era before grouping or
        # ranking scores.  Keep the aggregate columns in the index so the
        # frequently-polled activity snapshot can stay index-only as older
        # benchmark eras accumulate.
        Index(
            "scores_bench_version_agent_composite_idx",
            "bench_version",
            "agent_id",
            "composite",
            "validator_hotkey",
            postgresql_include=["updated_at"],
        ),
        Index(
            "scores_bench_timeline_idx",
            "bench_version",
            "agent_id",
            "updated_at",
            "validator_hotkey",
            postgresql_include=["memory_mean", "composite", "n"],
        ),
    )


class ConfirmationScore(Base):
    """One append-only shared-seed rescore result: a top-5 confirmation ledger row.

    The continual top-5 shared-seed rescore lane
    (``docs/top5-rescore-lane.md``) re-scores each emission-set member on the
    **champion-anchored** CRN seed set. Unlike :class:`Score` (the authoritative
    k=3 record, one upserted row per validator), this ledger is **append-only**:
    one immutable row per ``(agent_id, validator_hotkey, bench_version, seed)``,
    inserted with ``ON CONFLICT DO NOTHING`` and **never UPDATEd or deleted**. The
    longer a champion reigns, the more rows accumulate — the record grows
    monotonically and every seed's score is kept forever (auditable, no
    destructive read-modify-write).

    The KOTH fold (and its ``koth.py`` platform mirror) read the paired evidence
    from this history — grouped by seed, lower-median across validators — so
    "more seeds" is just "more rows to median over". The authoritative k=3
    ``scores`` table is untouched: no 4th ticket, no 4th ``Score`` PK row, no
    change to finalization. This ledger is a separate, additive store.
    """

    __tablename__ = "confirmation_scores"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """FK to ``agents.agent_id``. Unique-key part 1."""

    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    """SS58 hotkey of the reporting validator. Unique-key part 2."""

    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    """Benchmark semantics this signed rescore was produced under. Unique-key
    part 3 — the champion-anchored seed set is keyed on the major version."""

    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """The champion-anchored CRN seed the member was re-scored on. Unique-key
    part 4 — a validator contributes at most one composite per seed, immutably."""

    composite: Mapped[float] = mapped_column(Float, nullable=False)
    """Aggregate score in [0, 1] on this seed's dataset (as reported)."""

    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    """Scoring-engine run identifier for this seed's benchmark run."""

    signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The reporting validator's sr25519 signature over the parent score payload,
    hex encoded, so the ledger row is self-verifying alongside the k=3 receipt."""

    v9_efficiency_token_total: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    """Prompt-plus-completion token cost for a protocol-19 single-seed
    continual retest, whose signed v9 base-evidence digest is bound into this
    row's signature. Populated for any bench epoch that carries that evidence
    stack (v9 and every version after it). Null for historical, multi-seed, and
    non-confirmation rows."""

    v9_efficiency_cost_eligible: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )
    """Cost-evidence state for curve-v3 averaging: true with a positive token
    total, false for an explicit integrity failure, null when no new cost
    authority was available. An explicit false fails the agent's factor closed."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When this immutable row was appended (UTC). Never updated."""

    __table_args__ = (
        PrimaryKeyConstraint(
            "agent_id",
            "bench_version",
            "validator_hotkey",
            "seed",
            name="confirmation_scores_pkey",
        ),
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="confirmation_scores_agent_id_fkey",
        ),
        CheckConstraint(
            "composite >= 0 AND composite <= 1",
            name="confirmation_scores_composite_range_check",
        ),
        CheckConstraint(
            "bench_version > 0", name="confirmation_scores_bench_version_positive"
        ),
        # Same retired-era floor as ``scores``, and for the same reason: this is
        # a score ledger too. Also NOT VALID, so the existing v6 confirmation
        # rows survive untouched.
        CheckConstraint(
            "bench_version >= 7", name="confirmation_scores_bench_version_floor"
        ),
        CheckConstraint("seed >= 0", name="confirmation_scores_seed_check"),
        CheckConstraint(
            "(v9_efficiency_cost_eligible IS NULL "
            "AND v9_efficiency_token_total IS NULL) OR "
            "(v9_efficiency_cost_eligible = false "
            "AND v9_efficiency_token_total IS NULL) OR "
            "(v9_efficiency_cost_eligible = true "
            "AND bench_version >= 9 AND v9_efficiency_token_total > 0)",
            name="confirmation_scores_efficiency_cost_check",
        ),
        Index("confirmation_scores_agent_version_idx", "agent_id", "bench_version"),
    )


class EfficiencyCohortSnapshot(Base):
    """One frozen relative token-efficiency cohort (bench_version >= 7).

    The platform-side relative bonus (:mod:`ditto.api_server.efficiency`)
    freezes, once per efficiency epoch, the quality-qualified lineage-deduped
    cohort for a ``(bench_version, run_size)`` board together with its robust
    reference statistics (nearest-rank P25 frontier + median zero point) and
    the quality floors in force. Rows are **append-only and immutable**: a new
    epoch inserts a new row; nothing ever UPDATEs or deletes one, so every
    published bonus stays reproducible from stored data alone. The
    deterministic validator composite is never derived from or affected by
    this table.
    """

    __tablename__ = "efficiency_cohort_snapshots"

    snapshot_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    """Client-generated UUID; referenced by ``efficiency_bonuses`` rows."""

    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    """Benchmark contract the cohort was drawn from (always >= 7)."""

    run_size: Mapped[str] = mapped_column(Text, nullable=False)
    """Generator profile of the cohort's runs (``full`` for ranked boards)."""

    epoch_index: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Platform efficiency epoch ordinal (fixed UTC windows since the Unix
    epoch; see :func:`ditto.api_server.efficiency.epoch_index_for`)."""

    active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    """Whether the cohort met ``n_min`` after lineage dedupe. Inactive
    snapshots freeze the observation but award no bonus."""

    cohort_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    """Top-N cap on cohort membership in force at freeze time."""

    n_min: Mapped[int] = mapped_column(Integer, nullable=False)
    """Minimum deduped cohort size required for activation."""

    bonus_cap: Mapped[float] = mapped_column(Float, nullable=False)
    """Tier-1 maximum bonus fraction (``B_max``) frozen for this epoch: the
    value the curve reaches at the P25 frontier."""

    curve_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    """Bonus-curve policy this snapshot was frozen under
    (:data:`ditto.api_server.efficiency.CURVE_VERSION_SINGLE_TIER` /
    ``CURVE_VERSION_TWO_TIER``). Stored so a historical snapshot reproduces
    its bonuses under ITS policy forever, regardless of later curve changes."""

    deep_bonus_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Tier-2 saturation cap frozen for this epoch (two-tier curve only):
    the flat bonus at or below the deep frontier. ``>= bonus_cap``, <= 0.10.
    Null under the single-tier policy."""

    deep_frontier_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Deep frontier as a fraction of the P25 reference (two-tier curve
    only), in (0, 1). Null under the single-tier policy."""

    factor_alpha: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Exponent applied to the reference/agent token-cost ratio by a
    factor curve (v3/v4). Null for legacy v1/v2 snapshots."""

    minimum_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Lower multiplier bound frozen for a factor-curve snapshot. Curve v3
    applies it; v4 records it. Null for legacy v1/v2 snapshots."""

    maximum_factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Upper multiplier bound frozen for a factor-curve snapshot. Curve v3
    applies it; v4 records it. Null for legacy v1/v2 snapshots."""

    quality_floor: Mapped[float] = mapped_column(Float, nullable=False)
    """Composite floor (``Q_min``) applied at freeze time."""

    memory_floor: Mapped[float] = mapped_column(Float, nullable=False)
    """Memory-mean floor (``M_min``) applied at freeze time, so a harness
    cannot buy efficiency by sacrificing the memory half."""

    reference_p25_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Efficient-quartile frontier (nearest-rank P25 of cohort audited chat
    token totals); null while inactive."""

    reference_median_tokens: Mapped[float | None] = mapped_column(Float, nullable=True)
    """Cohort median of audited chat token totals (the zero-bonus point);
    null while inactive."""

    members: Mapped[list | None] = mapped_column(_JSON_VARIANT, nullable=True)
    """Frozen cohort membership: a list of ``{agent_id, miner_hotkey,
    lineage_key, composite, memory_mean, token_total, first_seen,
    collapsed_agent_ids}`` dicts, the full audit record a bonus is reproducible
    from. ``first_seen`` is absent on legacy snapshots and present on every new
    one. Lineage keys are moderation-adjacent digests; public projections expose
    only opaque group ordinals, never the raw keys."""

    computed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When this immutable snapshot was frozen (UTC). Never updated."""

    __table_args__ = (
        UniqueConstraint(
            "bench_version",
            "run_size",
            "epoch_index",
            name="efficiency_cohort_snapshots_epoch_key",
        ),
        # Redundant with snapshot_id's PK for uniqueness, but deliberately
        # names the exact referenced column set used by the assignment
        # identity FK below. This lets Postgres enforce that a bonus cannot
        # point at a snapshot from another benchmark or epoch.
        UniqueConstraint(
            "snapshot_id",
            "bench_version",
            "epoch_index",
            name="efficiency_cohort_snapshots_assignment_identity_key",
        ),
        CheckConstraint(
            "bench_version >= 7",
            name="efficiency_cohort_snapshots_bench_version_check",
        ),
        CheckConstraint(
            "bonus_cap > 0 AND bonus_cap <= 0.1",
            name="efficiency_cohort_snapshots_cap_check",
        ),
        CheckConstraint(
            "deep_bonus_cap IS NULL OR "
            "(deep_bonus_cap >= bonus_cap AND deep_bonus_cap <= 0.1)",
            name="efficiency_cohort_snapshots_deep_cap_check",
        ),
        CheckConstraint(
            "deep_frontier_ratio IS NULL OR "
            "(deep_frontier_ratio > 0 AND deep_frontier_ratio < 1)",
            name="efficiency_cohort_snapshots_deep_frontier_check",
        ),
        CheckConstraint(
            "curve_version >= 1",
            name="efficiency_cohort_snapshots_curve_version_check",
        ),
        CheckConstraint(
            "(curve_version IN (1, 2) AND factor_alpha IS NULL "
            "AND minimum_factor IS NULL AND maximum_factor IS NULL) OR "
            "(curve_version = 3 AND bench_version >= 9 "
            "AND deep_bonus_cap IS NULL AND deep_frontier_ratio IS NULL "
            "AND factor_alpha > 0 AND factor_alpha <= 1 "
            "AND minimum_factor >= 0.85 AND minimum_factor <= 1 "
            "AND maximum_factor >= 1 AND maximum_factor <= 1.1) OR "
            "(curve_version = 4 AND bench_version >= 9 "
            "AND deep_bonus_cap IS NULL AND deep_frontier_ratio IS NULL "
            "AND factor_alpha > 0 AND factor_alpha <= 1 "
            "AND minimum_factor > 0 AND minimum_factor <= 1 "
            "AND maximum_factor >= 1 AND maximum_factor <= 100 "
            "AND minimum_factor <= maximum_factor)",
            name="efficiency_cohort_snapshots_factor_parameters_check",
        ),
        CheckConstraint("n_min >= 2", name="efficiency_cohort_snapshots_n_min_check"),
        Index(
            "efficiency_cohort_snapshots_board_idx",
            "bench_version",
            "run_size",
            "epoch_index",
        ),
    )


class EfficiencyBonus(Base):
    """One agent's frozen relative token-efficiency adjustment (version >= 7).

    Insert-once per ``(agent_id, bench_version, epoch_index)``: assigned against
    the frozen cohort snapshot of that epoch (strictly-upside zero rows included,
    so "no bonus" is as frozen as "5%") and **never updated**, so a published
    effective score can never drift. Legacy curves derive
    ``effective_composite = composite * (1 + bonus)``; bounded curve v3 derives
    downside as ``composite * factor`` and upside against the remaining quality
    headroom. The validator's base composite is never modified.

    The key carries ``epoch_index`` because the bonus is *relative*: it is a
    position against a cohort's token distribution, and that distribution moves.
    Without the epoch in the key the row was insert-once per bench version
    FOREVER, so an agent that became more efficient could never earn a better
    bonus, and an agent measured mid-transition kept that measurement for the
    life of the contract. That happened at 04:43Z on 2026-07-26, part-way
    through the 4M -> 25M token-budget change.

    ``bench_version`` stays in the key and stays load-bearing: a bonus earned
    under one benchmark contract is meaningless under another (different
    dataset, different difficulty, different token profile). Epoch scoping is
    strictly *inside* version scoping, not a replacement for it.

    Recomputation is additive, never destructive. A new epoch writes a NEW row;
    prior epochs' rows are untouched, so ``/public/efficiency/snapshots/{id}``
    stays immutable and the audit trail is complete. The sole compatibility
    exception is a curve-v3 ``factor IS NULL, bonus = 0`` placeholder written
    by the immediately previous binary during deploy/rollback: it carries no
    scoring authority and may be promoted once to a signed factor by the v3
    writer. A database trigger neutralizes that old writer and rejects factors
    attached to legacy snapshots.
    """

    __tablename__ = "efficiency_bonuses"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """FK to ``agents.agent_id``. PK part 1."""

    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    """Benchmark contract of the bonused board. PK part 2."""

    epoch_index: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    """The epoch this bonus was computed in. PK part 3.

    Mirrors ``EfficiencyCohortSnapshot.epoch_index``, which has always carried
    it -- the asymmetry between the two tables was the defect. Backfilled from
    the referenced snapshot, so historical rows keep the epoch they were really
    assigned in rather than collapsing to zero.
    """

    snapshot_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """The frozen cohort snapshot this bonus was computed against (the
    ``bonus_reference`` provenance pointer)."""

    token_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    """The audited chat token total the bonus was computed from (median of
    the quorum's complete relay-metered totals); null when no quorum row
    carried complete audited usage (bonus is then necessarily 0)."""

    bonus: Mapped[float] = mapped_column(Float, nullable=False)
    """The frozen legacy additive bonus fraction in ``[0, 0.1]``; never
    negative. Bounded-factor rows keep this compatibility column at zero."""

    factor: Mapped[float | None] = mapped_column(Float, nullable=True)
    """The frozen efficiency multiplier. Curve v3 stays in ``[0.85, 1.10]``;
    curve v4 is any finite positive value. Null for legacy v1/v2 bonus rows."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When this immutable row was assigned (UTC). Never updated."""

    __table_args__ = (
        PrimaryKeyConstraint(
            "agent_id",
            "bench_version",
            "epoch_index",
            name="efficiency_bonuses_pkey",
        ),
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="efficiency_bonuses_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["snapshot_id"],
            ["efficiency_cohort_snapshots.snapshot_id"],
            name="efficiency_bonuses_snapshot_id_fkey",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "bench_version", "epoch_index"],
            [
                "efficiency_cohort_snapshots.snapshot_id",
                "efficiency_cohort_snapshots.bench_version",
                "efficiency_cohort_snapshots.epoch_index",
            ],
            name="efficiency_bonuses_snapshot_identity_fkey",
        ),
        CheckConstraint(
            "bonus >= 0 AND bonus <= 0.1", name="efficiency_bonuses_bonus_range_check"
        ),
        CheckConstraint(
            "factor IS NULL OR (bench_version >= 9 "
            "AND bonus = 0 AND token_total IS NOT NULL AND token_total > 0 "
            "AND token_total < 'Infinity'::double precision "
            "AND factor > 0 AND factor < 'Infinity'::double precision)",
            name="efficiency_bonuses_factor_range_check",
        ),
        CheckConstraint(
            "bench_version >= 7", name="efficiency_bonuses_bench_version_check"
        ),
        CheckConstraint(
            "epoch_index >= 0", name="efficiency_bonuses_epoch_index_check"
        ),
        Index("efficiency_bonuses_snapshot_idx", "snapshot_id"),
        # The fold and the board both ask "this agent, this version, this
        # epoch", which the PK already serves. This one serves the other
        # direction: sweep one epoch's whole board.
        Index("efficiency_bonuses_epoch_idx", "bench_version", "epoch_index"),
    )


class BenchmarkDataset(Base):
    """Immutable dataset pin for one agent and benchmark version."""

    __tablename__ = "benchmark_datasets"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    run_size: Mapped[str] = mapped_column(Text, nullable=False)
    seed_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    seed_block_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "agent_id", "bench_version", name="benchmark_datasets_pkey"
        ),
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        CheckConstraint("bench_version > 0", name="benchmark_dataset_version_positive"),
        CheckConstraint("length(sha256) = 64", name="benchmark_dataset_sha_length"),
    )


class BenchmarkRollout(Base):
    """Durable benchmark transition snapshot; there is at most one open row."""

    __tablename__ = "benchmark_rollouts"

    rollout_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    cohort_size: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    rescore_cohort_target: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default=text("10")
    )
    """How many inherited agents this rollout set out to rescore.

    Frozen from the operator setting
    (``queue_policy_settings_revisions.settings.rescore_cohort_size``) at
    START and never rewritten, so a later revision cannot resize an in-flight
    rollout and a historical rollout stays explainable. ``cohort_size`` is how
    many members were actually qualified; this is the size that was aimed for.
    """
    priority_cohort_target: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=text("5")
    )
    """How many top inherited positions must hold a target-version quorum here.

    Frozen from ``queue_policy_settings_revisions.settings.priority_cohort_size``
    at START, for the same reason as ``rescore_cohort_target``: this is the gate
    that decides when the rollout may take ledger authority, and re-gating a
    transition already in flight would move the finish line underneath it.

    Distinct from ``ditto.db.queries.benchmark_rollout.PRIORITY_COHORT_SIZE``,
    which stays a constant. That constant serves two fused meanings -- the
    rollout activation gate AND the KOTH top-five emission set. Only the former
    is queue policy; the latter is a consensus quantity that must keep matching
    ``MIN_DESIRED_AUTHORITY_AGENTS``. This column is the tunable half, and the
    constant remains its floor.
    """
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("from_version > 0", name="benchmark_rollout_from_positive"),
        CheckConstraint(
            "desired_version > from_version", name="benchmark_rollout_forward"
        ),
        # No new rollout may ever target a retired era. Combined with
        # ``benchmark_rollout_forward`` above (desired > from) this also pins
        # ``from_version`` at 6 or higher for any NEW row, so the only rollout
        # this table can still open is one that ends at v7 or later.
        #
        # NOT VALID in the migration: the six historical rows -- including the
        # v6->v7 transition whose ``from_version`` is 6 -- stay exactly as they
        # are, because they are the audit trail of how the fleet got here.
        CheckConstraint("desired_version >= 7", name="benchmark_rollout_desired_floor"),
        CheckConstraint(
            # Retain the historical storage bound for rollout snapshots created
            # before new transitions were narrowed to ten members.
            "cohort_size BETWEEN 5 AND 25",
            name="benchmark_rollout_bounded_members",
        ),
        CheckConstraint(
            # Same storage ceiling as cohort_size: an operator may widen the
            # target from the default ten up to twenty-five, never past it.
            # Deliberately NOT related to cohort_size by a constraint --
            # pre-existing 11-25 member rollouts backfilled a target that is
            # their own size, and an in-flight rollout is legitimately below it.
            "rescore_cohort_target BETWEEN 5 AND 25",
            name="benchmark_rollout_bounded_rescore_target",
        ),
        CheckConstraint(
            # Floor is the KOTH emission-set size: a priority gate below five
            # would let the ledger flip to the desired version with fewer
            # recipients than the emission split expects. Ceiling is the same
            # storage bound as the rescore target. Deliberately NOT constrained
            # to be <= rescore_cohort_target in SQL: the wire model already
            # rejects that combination, and a CHECK spanning both columns would
            # make backfilling a legacy 11-25-member rollout order-dependent.
            "priority_cohort_target BETWEEN 5 AND 25",
            name="benchmark_rollout_bounded_priority_target",
        ),
        CheckConstraint(
            # 'superseded' is terminal: an operator abandoned the rollout before
            # activation. The partial open index below excludes it, so it frees
            # the single open slot.
            "status IN ('collecting', 'blocked_ineligible', 'activated', 'superseded')",
            name="benchmark_rollout_status",
        ),
        Index(
            "benchmark_rollouts_one_open_idx",
            text("(1)"),
            unique=True,
            postgresql_where=text("status IN ('collecting', 'blocked_ineligible')"),
            sqlite_where=text("status IN ('collecting', 'blocked_ineligible')"),
        ),
        Index(
            "benchmark_rollouts_transition_idx",
            "from_version",
            "desired_version",
            unique=True,
        ),
    )


class BenchmarkRolloutMember(Base):
    """An agent qualified during a rolling benchmark activation."""

    __tablename__ = "benchmark_rollout_members"

    rollout_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    frozen_miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    frozen_composite: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("rollout_id", "agent_id"),
        UniqueConstraint("rollout_id", "position"),
        ForeignKeyConstraint(
            ["rollout_id"], ["benchmark_rollouts.rollout_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        CheckConstraint("position > 0", name="benchmark_member_position"),
    )


class BenchmarkRolloutCarryover(Base):
    """A previous-generation submission the new benchmark era adopted.

    Deliberately a **separate table** from :class:`BenchmarkRolloutMember`
    rather than a ``kind`` discriminator on it. Every existing query that counts
    or lists rollout members treats "row in ``benchmark_rollout_members``" as
    "inherited rescore cohort": ``cohort_size``, ``rollout_cohort_complete``,
    ``rollout_cohort_score_complete``, ``_validate_frozen_members``,
    ``rollout_state``'s member list, ``active_bench_version``,
    ``authority_selection_state``, and ``issue_rollout_ticket``. A discriminator
    would require a ``kind`` filter on every one of them, and missing a single
    filter would silently re-gate an open rollout's activation on agents that
    were never part of its cohort. A separate table cannot be accidentally
    included by any of those queries.

    The row is the sole admission credential for a carried-over submission (see
    ``ditto.db.queries.benchmark_admission.benchmark_admission_predicate``), and
    it is only ever written in the same transaction that pins the agent's
    desired-version :class:`BenchmarkDataset`. That is what makes admission and
    dataset generation inseparable rather than merely coordinated.
    """

    __tablename__ = "benchmark_rollout_carryover"

    rollout_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    """1-based adoption order, so the operator cap is auditable after the fact."""
    frozen_score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    """Prior-era accepted score count at adoption time, for audit."""
    frozen_owner_key: Mapped[str] = mapped_column(Text, nullable=False)
    """The owner key this row was deduped under (``coldkey:``/``hotkey:``)."""
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("rollout_id", "agent_id"),
        UniqueConstraint("rollout_id", "position"),
        ForeignKeyConstraint(
            ["rollout_id"], ["benchmark_rollouts.rollout_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        CheckConstraint("position > 0", name="benchmark_carryover_position"),
        CheckConstraint(
            "frozen_score_count BETWEEN 0 AND 2",
            name="benchmark_carryover_frozen_score_count",
        ),
    )


class BenchmarkRolloutAudit(Base):
    """Append-only operator/public-safe history for benchmark transitions."""

    __tablename__ = "benchmark_rollout_audit"

    audit_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    rollout_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    event: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["rollout_id"], ["benchmark_rollouts.rollout_id"], ondelete="CASCADE"
        ),
        Index("benchmark_rollout_audit_history_idx", "rollout_id", "recorded_at"),
    )


class ValidatorHeartbeat(Base):
    """Latest signed build and runtime heartbeat for one validator hotkey."""

    __tablename__ = "validator_heartbeats"

    validator_hotkey: Mapped[str] = mapped_column(Text, primary_key=True)
    software_version: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    code_digest: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    active_agent_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    first_seen_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    system_metrics: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    benchmark_progress: Mapped[dict | None] = mapped_column(
        _JSON_VARIANT, nullable=True
    )
    benchmark_progress_reported: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    benchmark_progress_agent_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    capabilities: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    stack: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    stack_health: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    updater_status: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    benchmark_capacity: Mapped[dict | None] = mapped_column(
        _JSON_VARIANT, nullable=True
    )
    confirmation_progress: Mapped[list | None] = mapped_column(
        _JSON_VARIANT, nullable=True
    )
    # The slots the validator's SIGNED capacity claimed as busy, recorded before
    # the ticket-confirmation filter in ``_validated_heartbeat_work`` narrows
    # ``benchmark_capacity`` to work the ledger could confirm. A slot can be
    # genuinely occupied and still fail confirmation (a re-issued lease moves the
    # deadline the validator cached, so the exact-deadline lookup misses), and
    # ``benchmark_capacity`` alone cannot tell "this slot is free" apart from
    # "this slot's progress did not confirm". Only ever used to REFUSE a
    # revocation, never to grant work or accept a score, so an over-broad claim
    # costs the validator its own throughput and nothing else.
    # Shape: ``[{"slot_id": "slot-0", "agent_id": "<uuid>"}, ...]``.
    claimed_slots: Mapped[list | None] = mapped_column(_JSON_VARIANT, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "length(software_version) BETWEEN 1 AND 64",
            name="validator_heartbeats_software_version_length_check",
        ),
        CheckConstraint(
            "protocol_version > 0",
            name="validator_heartbeats_protocol_version_check",
        ),
        CheckConstraint(
            "length(code_digest) = 64",
            name="validator_heartbeats_code_digest_length_check",
        ),
        CheckConstraint(
            "state IN ('polling', 'running_benchmark', 'updating_weights', "
            "'idle', 'error', 'paused')",
            name="validator_heartbeats_state_check",
        ),
        CheckConstraint(
            "length(signature) = 128",
            name="validator_heartbeats_signature_length_check",
        ),
        ForeignKeyConstraint(
            ["active_agent_id"],
            ["agents.agent_id"],
            ondelete="SET NULL",
            name="validator_heartbeats_active_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["benchmark_progress_agent_id"],
            ["agents.agent_id"],
            ondelete="SET NULL",
            name="validator_heartbeats_benchmark_progress_agent_id_fkey",
        ),
        Index("validator_heartbeats_seen_at_idx", "seen_at"),
        Index(
            "validator_heartbeats_active_agent_idx",
            "active_agent_id",
            postgresql_where=text("active_agent_id IS NOT NULL"),
        ),
    )


class ScreenerHeartbeat(Base):
    """Latest signed runtime and host-health report for one screener instance.

    Keyed by (screener_hotkey, instance_id): the prod fleet shares one hotkey,
    so instance_id (the worker's GCE instance name) is what keeps each worker a
    distinct row instead of collapsing the fleet into one. Pre-v3 workers that
    send no instance_id are stored under the ``"legacy"`` sentinel.
    """

    __tablename__ = "screener_heartbeats"

    screener_hotkey: Mapped[str] = mapped_column(Text, primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        Text, primary_key=True, server_default="legacy"
    )
    software_version: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    active_agent_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    first_seen_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    system_metrics: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    reported_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    seen_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "length(software_version) BETWEEN 1 AND 64",
            name="screener_heartbeats_software_version_length_check",
        ),
        CheckConstraint(
            "length(instance_id) BETWEEN 1 AND 63",
            name="screener_heartbeats_instance_id_length_check",
        ),
        CheckConstraint(
            "protocol_version > 0",
            name="screener_heartbeats_protocol_version_check",
        ),
        CheckConstraint(
            "policy_version > 0",
            name="screener_heartbeats_policy_version_check",
        ),
        CheckConstraint(
            "state IN ('polling', 'screening', 'error', 'paused')",
            name="screener_heartbeats_state_check",
        ),
        CheckConstraint(
            "length(signature) = 128",
            name="screener_heartbeats_signature_length_check",
        ),
        ForeignKeyConstraint(
            ["active_agent_id"],
            ["agents.agent_id"],
            ondelete="SET NULL",
            name="screener_heartbeats_active_agent_id_fkey",
        ),
        Index("screener_heartbeats_seen_at_idx", "seen_at"),
        Index(
            "screener_heartbeats_active_agent_idx",
            "active_agent_id",
            postgresql_where=text("active_agent_id IS NOT NULL"),
        ),
    )


class ScreenerNodeBootstrapGrant(Base):
    """One short-lived, single-use controller-minted enrollment capability."""

    __tablename__ = "screener_node_bootstrap_grants"

    grant_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    controller_epoch: Mapped[str] = mapped_column(Text, nullable=False)
    image_reference: Mapped[str | None] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    registration_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_node_bootstrap_grants_environment_check",
        ),
        CheckConstraint(
            "length(node_id) BETWEEN 1 AND 63",
            name="screener_node_bootstrap_grants_node_id_length_check",
        ),
        CheckConstraint(
            "provider IN ('gcp', 'targon', 'hetzner', 'home', 'test')",
            name="screener_node_bootstrap_grants_provider_check",
        ),
        CheckConstraint(
            "length(token_hash) = 64",
            name="screener_node_bootstrap_grants_token_hash_check",
        ),
        CheckConstraint(
            "image_reference IS NULL OR image_reference ~ "
            "'^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$'",
            name="screener_node_bootstrap_grants_image_reference_check",
        ),
        Index("screener_node_bootstrap_grants_node_idx", "node_id", "created_at"),
    )


class ScreenerNode(Base):
    """A uniquely enrolled worker with revocable, rotating authority."""

    __tablename__ = "screener_nodes"

    environment: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    screener_hotkey: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    previous_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_token_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_refresh_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    token_expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    capacity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    image_reference: Mapped[str | None] = mapped_column(Text)
    registered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    rotated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    status_reason: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_nodes_environment_check",
        ),
        CheckConstraint(
            "length(node_id) BETWEEN 1 AND 63",
            name="screener_nodes_node_id_length_check",
        ),
        CheckConstraint(
            "provider IN ('gcp', 'targon', 'hetzner', 'home', 'test')",
            name="screener_nodes_provider_check",
        ),
        CheckConstraint(
            "length(token_hash) = 64", name="screener_nodes_token_hash_check"
        ),
        CheckConstraint(
            "previous_token_hash IS NULL OR length(previous_token_hash) = 64",
            name="screener_nodes_previous_token_hash_check",
        ),
        CheckConstraint(
            "status IN ('active', 'draining', 'quarantined', 'revoked')",
            name="screener_nodes_status_check",
        ),
        CheckConstraint(
            "capacity BETWEEN 1 AND 16", name="screener_nodes_capacity_check"
        ),
        CheckConstraint(
            "image_reference IS NULL OR image_reference ~ "
            "'^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$'",
            name="screener_nodes_image_reference_check",
        ),
        Index("screener_nodes_provider_status_idx", "provider", "status"),
        UniqueConstraint(
            "environment",
            "provider",
            "provider_resource_id",
            name="screener_nodes_provider_resource_key",
        ),
    )


class ScreenerCapacitySnapshot(Base):
    """Latest fenced capacity-reconciler state for one environment."""

    __tablename__ = "screener_capacity_snapshots"

    environment: Mapped[str] = mapped_column(Text, primary_key=True)
    controller_epoch: Mapped[str] = mapped_column(Text, nullable=False)
    controller_source_sha: Mapped[str] = mapped_column(Text, nullable=False)
    provider_settings_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    provider_ready: Mapped[bool] = mapped_column(Boolean, nullable=False)
    controller_heartbeat_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    controller_lease_expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    runnable_backlog: Mapped[int] = mapped_column(Integer, nullable=False)
    active_leases: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    global_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    targon_capability: Mapped[str] = mapped_column(Text, nullable=False)
    targon_available: Mapped[int] = mapped_column(Integer, nullable=False)
    targon_healthy: Mapped[int] = mapped_column(Integer, nullable=False)
    targon_pending: Mapped[int] = mapped_column(Integer, nullable=False)
    targon_draining: Mapped[int] = mapped_column(Integer, nullable=False)
    gce_target: Mapped[int] = mapped_column(Integer, nullable=False)
    gce_healthy: Mapped[int] = mapped_column(Integer, nullable=False)
    gce_pending: Mapped[int] = mapped_column(Integer, nullable=False)
    gce_draining: Mapped[int] = mapped_column(Integer, nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(Text)
    last_provider_success_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    last_provider_error_code: Mapped[str | None] = mapped_column(Text)
    last_provider_error_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "targon_capability IN ('go', 'nogo', 'unknown')",
            name="screener_capacity_snapshots_targon_capability_check",
        ),
        CheckConstraint(
            "controller_source_sha ~ '^[0-9a-f]{40}$'",
            name="screener_capacity_snapshots_source_sha_check",
        ),
        CheckConstraint(
            "provider_settings_revision >= 0",
            name="screener_capacity_snapshots_provider_revision_check",
        ),
        CheckConstraint(
            "runnable_backlog >= 0 AND active_leases >= 0 AND "
            "desired_slots >= 0 AND global_cap >= 0",
            name="screener_capacity_snapshots_nonnegative_demand_check",
        ),
        CheckConstraint(
            "targon_available >= 0 AND targon_healthy >= 0 AND "
            "targon_pending >= 0 AND targon_draining >= 0",
            name="screener_capacity_snapshots_nonnegative_targon_check",
        ),
        CheckConstraint(
            "gce_target >= 0 AND gce_healthy >= 0 AND "
            "gce_pending >= 0 AND gce_draining >= 0",
            name="screener_capacity_snapshots_nonnegative_gce_check",
        ),
    )


class ScreenerCapacityEvent(Base):
    """Append-only provider/controller audit event."""

    __tablename__ = "screener_capacity_events"

    event_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text)
    node_id: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    controller_epoch: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "provider IS NULL OR "
            "provider IN ('gcp', 'targon', 'hetzner', 'home', 'test')",
            name="screener_capacity_events_provider_check",
        ),
        Index(
            "screener_capacity_events_environment_created_idx",
            "environment",
            "created_at",
        ),
    )


class ScreenerProviderSettingsRevision(Base):
    """Append-only operator routing for screening and remote-build providers."""

    __tablename__ = "screener_provider_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_provider_settings_environment_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="screener_provider_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screener_provider_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_provider_settings_actor_check",
        ),
        Index(
            "screener_provider_settings_environment_revision_idx",
            "environment",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "environment",
            "parent_revision",
            name="screener_provider_settings_environment_parent_key",
        ),
    )


class ScreenerNodeChannelSettingsRevision(Base):
    """Append-only Platform admission limits for one enrolled node."""

    __tablename__ = "screener_node_channel_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["node_id"],
            ["screener_nodes.node_id"],
            ondelete="CASCADE",
            name="screener_node_channel_settings_node_id_fkey",
        ),
        CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="screener_node_channel_settings_environment_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="screener_node_channel_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screener_node_channel_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_node_channel_settings_actor_check",
        ),
        Index(
            "screener_node_channel_settings_node_revision_idx",
            "node_id",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "node_id",
            "parent_revision",
            name="screener_node_channel_settings_node_parent_key",
        ),
    )


class TrustedImageBuild(Base):
    """Audited, controller-leased build of one allowlisted monorepo image."""

    __tablename__ = "trusted_image_builds"

    build_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    component: Mapped[str] = mapped_column(Text, nullable=False)
    source_repository: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha: Mapped[str] = mapped_column(Text, nullable=False)
    context_path: Mapped[str] = mapped_column(Text, nullable=False)
    dockerfile_path: Mapped[str] = mapped_column(Text, nullable=False)
    destination: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    provider: Mapped[str | None] = mapped_column(Text)
    provider_resource_id: Mapped[str | None] = mapped_column(Text)
    image_digest: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    controller_epoch: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="trusted_image_builds_environment_check",
        ),
        CheckConstraint(
            "component IN ('screener')",
            name="trusted_image_builds_component_check",
        ),
        CheckConstraint(
            "source_sha ~ '^[0-9a-f]{40}$'",
            name="trusted_image_builds_source_sha_check",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', 'failed', "
            "'fallback_required', 'canceled')",
            name="trusted_image_builds_status_check",
        ),
        CheckConstraint(
            "provider IS NULL OR provider IN ('targon', 'gcp', 'hetzner')",
            name="trusted_image_builds_provider_check",
        ),
        CheckConstraint(
            "image_digest IS NULL OR image_digest ~ '^sha256:[0-9a-f]{64}$'",
            name="trusted_image_builds_digest_check",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="trusted_image_builds_attempt_count_check",
        ),
        UniqueConstraint(
            "environment",
            "component",
            "source_sha",
            name="trusted_image_builds_source_key",
        ),
        Index(
            "trusted_image_builds_queue_idx",
            "environment",
            "status",
            "created_at",
        ),
    )


class SubmissionImageBuild(Base):
    """Attempt-bound provider build whose output is independently screened."""

    __tablename__ = "submission_image_builds"

    build_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    image_ref: Mapped[str] = mapped_column(Text, nullable=False)
    output_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    provider: Mapped[str | None] = mapped_column(Text)
    node_id: Mapped[str | None] = mapped_column(Text)
    provider_resource_id: Mapped[str | None] = mapped_column(Text)
    output_sha256: Mapped[str | None] = mapped_column(Text)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    output_image_id: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    runtime_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="skipped"
    )
    runtime_node_id: Mapped[str | None] = mapped_column(Text)
    runtime_provider_resource_id: Mapped[str | None] = mapped_column(Text)
    runtime_image_reference: Mapped[str | None] = mapped_column(Text)
    runtime_error_code: Mapped[str | None] = mapped_column(Text)
    runtime_completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    controller_epoch: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    job_token_hash: Mapped[str | None] = mapped_column(Text)
    job_token_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    upload_minted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="submission_image_builds_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
            name="submission_image_builds_attempt_id_fkey",
        ),
        ForeignKeyConstraint(
            ["node_id"],
            ["screener_nodes.node_id"],
            ondelete="SET NULL",
            name="submission_image_builds_node_id_fkey",
        ),
        ForeignKeyConstraint(
            ["runtime_node_id"],
            ["screener_nodes.node_id"],
            ondelete="SET NULL",
            name="submission_image_builds_runtime_node_id_fkey",
        ),
        CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="submission_image_builds_environment_check",
        ),
        CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="submission_image_builds_artifact_sha_check",
        ),
        CheckConstraint(
            "image_ref = 'ditto-screen/' || agent_id::text || '-' || "
            "attempt_id::text || chr(58) || 'latest'",
            name="submission_image_builds_image_ref_check",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', "
            "'fallback_required', 'canceled', 'consumed')",
            name="submission_image_builds_status_check",
        ),
        CheckConstraint(
            "provider IS NULL OR provider IN ('targon', 'gcp', 'hetzner')",
            name="submission_image_builds_provider_check",
        ),
        CheckConstraint(
            "runtime_status IN ('pending', 'running', 'succeeded', "
            "'fallback_required', 'skipped')",
            name="submission_image_builds_runtime_status_check",
        ),
        CheckConstraint(
            "runtime_image_reference IS NULL OR runtime_image_reference ~ "
            "'^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$'",
            name="submission_image_builds_runtime_image_check",
        ),
        CheckConstraint(
            "output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$'",
            name="submission_image_builds_output_sha_check",
        ),
        CheckConstraint(
            "output_image_id IS NULL OR output_image_id ~ '^sha256:[0-9a-f]{64}$'",
            name="submission_image_builds_output_image_id_check",
        ),
        CheckConstraint(
            "output_size_bytes IS NULL OR output_size_bytes BETWEEN 1 AND 4294967296",
            name="submission_image_builds_output_size_check",
        ),
        CheckConstraint(
            "job_token_hash IS NULL OR job_token_hash ~ '^[0-9a-f]{64}$'",
            name="submission_image_builds_token_hash_check",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="submission_image_builds_attempt_count_check",
        ),
        UniqueConstraint("attempt_id", name="submission_image_builds_attempt_key"),
        Index(
            "submission_image_builds_queue_idx",
            "environment",
            "status",
            "created_at",
        ),
        Index("submission_image_builds_node_status_idx", "node_id", "status"),
        Index(
            "submission_image_builds_runtime_node_status_idx",
            "runtime_node_id",
            "runtime_status",
        ),
    )


class SubmissionSourceReview(Base):
    """Attempt-bound, provider-routed read-only source review."""

    __tablename__ = "submission_source_reviews"

    review_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    provider: Mapped[str | None] = mapped_column(Text)
    node_id: Mapped[str | None] = mapped_column(Text)
    provider_resource_id: Mapped[str | None] = mapped_column(Text)
    observation: Mapped[dict | None] = mapped_column(_JSON_VARIANT)
    error_code: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    controller_epoch: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    job_token_hash: Mapped[str | None] = mapped_column(Text)
    job_token_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    provider_outage_epoch: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    """Relay circuit epoch that parked this review without spending an attempt."""
    provider_outage_attempted_epoch: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    """Last outage epoch whose one refunded resume this review already used."""
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="submission_source_reviews_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
            name="submission_source_reviews_attempt_id_fkey",
        ),
        ForeignKeyConstraint(
            ["node_id"],
            ["screener_nodes.node_id"],
            ondelete="SET NULL",
            name="submission_source_reviews_node_id_fkey",
        ),
        CheckConstraint(
            "environment ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="submission_source_reviews_environment_check",
        ),
        CheckConstraint(
            "artifact_sha256 ~ '^[0-9a-f]{64}$'",
            name="submission_source_reviews_artifact_sha_check",
        ),
        CheckConstraint(
            "status IN ('queued', 'leased', 'running', 'succeeded', "
            "'fallback_required', 'canceled', 'consumed')",
            name="submission_source_reviews_status_check",
        ),
        CheckConstraint(
            "provider IS NULL OR provider IN ('targon', 'gcp', 'hetzner')",
            name="submission_source_reviews_provider_check",
        ),
        CheckConstraint(
            "job_token_hash IS NULL OR job_token_hash ~ '^[0-9a-f]{64}$'",
            name="submission_source_reviews_token_hash_check",
        ),
        CheckConstraint(
            "attempt_count BETWEEN 0 AND 3",
            name="submission_source_reviews_attempt_count_check",
        ),
        UniqueConstraint("attempt_id", name="submission_source_reviews_attempt_key"),
        Index(
            "submission_source_reviews_queue_idx",
            "environment",
            "status",
            "created_at",
        ),
        Index("submission_source_reviews_node_status_idx", "node_id", "status"),
    )


class ScreenerReviewSettingsRevision(Base):
    """Append-only, operator-audited L2/L3 settings revision."""

    __tablename__ = "screener_review_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "scope = '*' OR length(scope) BETWEEN 1 AND 63",
            name="screener_review_settings_scope_check",
        ),
        CheckConstraint(
            "length(checksum) = 64",
            name="screener_review_settings_checksum_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screener_review_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_review_settings_actor_check",
        ),
        Index(
            "screener_review_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "scope",
            "parent_revision",
            name="screener_review_settings_scope_parent_key",
        ),
    )


class ArtifactReleaseSettingsRevision(Base):
    """Append-only, operator-audited public source embargo revision."""

    __tablename__ = "artifact_release_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    disclosure: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'public'")
    )
    """Whether the public release path is reachable at all (``public`` |
    ``never``).

    Values are :class:`~ditto.api_models.source_disclosure.SourceDisclosure`;
    the check constraint is the authority. Typed ``str`` rather than a
    PostgreSQL ENUM on purpose -- adding a value to a native enum needs its own
    migration, whereas a CHECK constraint is a one-line swap, and the set of
    release policies is exactly the kind of thing that grows. Not nullable: the
    one thing a privacy setting must never be is ambiguous.
    """

    embargo_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    """The window in hours. Stays ``NOT NULL`` and in range under every policy,
    including ``never``, where it is retained but inert."""

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "embargo_hours BETWEEN 6 AND 8760",
            name="artifact_release_settings_embargo_hours_check",
        ),
        CheckConstraint(
            "disclosure IN ('public', 'never')",
            name="artifact_release_settings_disclosure_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="artifact_release_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="artifact_release_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="artifact_release_settings_actor_check",
        ),
        UniqueConstraint(
            "parent_revision",
            name="artifact_release_settings_parent_revision_key",
        ),
    )


class SubmissionSettingsRevision(Base):
    """Append-only, operator-audited miner submission settings revision."""

    __tablename__ = "submission_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    fee_amount_rao: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name="submission_settings_cooldown_seconds_check",
        ),
        CheckConstraint(
            "fee_amount_rao BETWEEN 1 AND 1000000000000",
            name="submission_settings_fee_amount_rao_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="submission_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="submission_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="submission_settings_actor_check",
        ),
        UniqueConstraint(
            "parent_revision",
            name="submission_settings_parent_revision_key",
        ),
    )


class SubmissionDepositAddressRevision(Base):
    """Append-only, operator-audited upload payment destination revision."""

    __tablename__ = "submission_deposit_address_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_address: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "payment_address ~ '^[1-9A-HJ-NP-Za-km-z]{32,64}$'",
            name="submission_deposit_address_ss58_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="submission_deposit_address_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="submission_deposit_address_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="submission_deposit_address_actor_check",
        ),
        UniqueConstraint(
            "parent_revision",
            name="submission_deposit_address_parent_revision_key",
        ),
    )


class UploadAdmissionReservation(Base):
    """Short-lived pre-payment reservation for one payment coldkey."""

    __tablename__ = "upload_admission_reservations"

    miner_coldkey: Mapped[str] = mapped_column(Text, primary_key=True)
    token: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), unique=True, nullable=False
    )
    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    fee_amount_rao: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payment_send_address: Mapped[str | None] = mapped_column(Text, nullable=True)

    legacy_payment_cutoff_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    """Latest block time eligible for the one-time legacy-fee transition."""
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "cooldown_seconds BETWEEN 60 AND 86400",
            name="upload_admission_cooldown_seconds_check",
        ),
        CheckConstraint(
            "fee_amount_rao BETWEEN 1 AND 1000000000000",
            name="upload_admission_fee_amount_rao_check",
        ),
        CheckConstraint(
            "length(sha256) = 64",
            name="upload_admission_sha256_length_check",
        ),
        Index("upload_admission_expires_at_idx", "expires_at"),
    )


class AgentKingship(Base):
    """King-reign ledger gating public source release, in two write-once stages.

    ``first_crowned_at`` marks that the agent was ever the KOTH champion
    (eligibility). ``weight_confirmed_at`` marks the first time validators'
    REVEALED on-chain weights (post commit-reveal) were seen set on this miner;
    it is ``NULL`` until then. The public window is king-only and measured from
    ``weight_confirmed_at`` -- so a genuine, on-chain-backed king reveals one
    window later, while an agent that merely touched the crown off-chain waits
    until the chain confirms it. Both timestamps are write-once and never move.
    Written on the validator score path (post-commit); the gate only reads them.
    """

    __tablename__ = "agent_kingship"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    first_crowned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    weight_confirmed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
    )


class ScreenerShadowReview(Base):
    """Non-authoritative L2/L3 telemetry bound to one live attempt."""

    __tablename__ = "screener_shadow_reviews"

    attempt_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    screener_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    disposition: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str | None] = mapped_column(Text, nullable=True)
    categories: Mapped[list] = mapped_column(_JSON_VARIANT, nullable=False)
    finding_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    clearance_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    critic_disposition: Mapped[str | None] = mapped_column(Text, nullable=True)
    adjudicator_disposition: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_models: Mapped[list] = mapped_column(_JSON_VARIANT, nullable=False)
    response_providers: Mapped[list] = mapped_column(_JSON_VARIANT, nullable=False)
    usage: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
            name="screener_shadow_reviews_attempt_id_fkey",
        ),
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="screener_shadow_reviews_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["settings_revision"],
            ["screener_review_settings_revisions.revision"],
            ondelete="RESTRICT",
            name="screener_shadow_reviews_settings_revision_fkey",
        ),
        CheckConstraint(
            "length(artifact_sha256) = 64",
            name="screener_shadow_reviews_artifact_sha_check",
        ),
        CheckConstraint(
            "length(settings_checksum) = 64",
            name="screener_shadow_reviews_settings_checksum_check",
        ),
        CheckConstraint(
            "disposition IN ('safe', 'violation', 'inconclusive', 'retryable_infra')",
            name="screener_shadow_reviews_disposition_check",
        ),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('low', 'medium', 'high')",
            name="screener_shadow_reviews_risk_check",
        ),
        Index("screener_shadow_reviews_created_idx", "created_at"),
        Index("screener_shadow_reviews_agent_idx", "agent_id", "created_at"),
    )


class ValidatorTicket(Base):
    """One validator's evaluation ticket for one agent (a k=3 scoring grant).

    A submission is scored by exactly three validators. The platform issues at
    most three tickets per agent, each to a *distinct* validator hotkey, and
    refuses further requests ("no job for you"). A ticket is the right to score:
    the validator loads the agent + the platform-generated dataset, scores it,
    and must post the signed score back before ``deadline`` or the ticket
    expires and its slot re-opens for another validator.

    The composite primary key ``(agent_id, validator_hotkey)`` enforces
    distinctness — a validator can hold at most one ticket per agent, so it can
    never occupy two of the three slots and skew the median. ``agent_id`` is a
    single-column FK to ``agents.agent_id`` with ``ON DELETE CASCADE`` because a
    ticket is derived from a live submission.

    The three composites themselves live in :class:`Score` (one row per
    ``(agent, validator)``); a ticket tracks only issuance, the deadline, and
    lifecycle. An agent finalizes (median-of-three) once it has three
    ``scored`` tickets.
    """

    __tablename__ = "validator_tickets"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """FK to ``agents.agent_id``. PK part 1."""

    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    """SS58 hotkey of the ticket-holding validator. PK part 2 (distinctness)."""

    slot_id: Mapped[str] = mapped_column(
        Text, nullable=False, default="slot-0", server_default="slot-0"
    )
    """Heartbeat-v10 execution slot holding this live lease."""

    status: Mapped[TicketStatus] = mapped_column(
        Enum(
            TicketStatus,
            name="ticketstatus",
            values_callable=lambda enum_cls: [m.value for m in enum_cls],
            create_constraint=True,
        ),
        nullable=False,
        server_default=text("'issued'"),
    )
    """Current state: ``issued`` -> ``scored`` | ``expired``."""

    purpose: Mapped[TicketPurpose] = mapped_column(
        Text,
        nullable=False,
        default=TicketPurpose.CANONICAL_QUORUM,
        server_default=text("'legacy_unclassified'"),
    )
    """Why the current lease was issued; old-writer leases remain unclassified."""

    purpose_revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("0"),
    )
    """Positive only when a purpose-aware writer classified this lease."""

    legacy_completion_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        # Old application replicas omit this column during the bounded rolling
        # transition. Purpose-aware writers always send the Python default.
        server_default=text("true"),
    )
    """Bounded migration-only allowance for leases already live at cutover."""

    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When the ticket was granted (UTC)."""

    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    """When an unscored ticket expires and its slot re-opens (UTC)."""

    # Defaulted to the floor for the same reason as ``Score.bench_version``: a
    # default of 2 is a value this table's own
    # ``validator_tickets_bench_version_floor`` trigger refuses, so it could
    # only ever produce a runtime error at INSERT. Every production caller
    # passes this explicitly.
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    """Benchmark version whose retry budget this ticket consumes."""

    seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Block-hash-derived dataset seed assigned to this validator run."""

    dataset_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Digest of the dataset rendered from :attr:`seed`."""

    seed_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    """Finalized chain block used for the validator-specific seed."""

    seed_block_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Hash used to independently re-derive the validator-specific seed."""

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    """Number of leases issued to this validator for this agent/version."""

    manual_retry_grants: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """Audited operator grants that each allow one additional lease after the
    single base attempt is exhausted. Grants never reduce or
    rewrite :attr:`attempt_count`; the append-only recovery row records why the
    extra eligibility was created."""

    infra_retry_grants: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    """Historical automatic-era cap extensions, retained as audit evidence.

    Current issuance deliberately ignores this value. Only
    :attr:`manual_retry_grants` can extend the attempt cap."""

    retry_after: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    """Earliest time this validator may retry an expired ticket."""

    provider_outage_epoch: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    """Relay circuit epoch that parked this lease without minting a retry grant."""

    provider_outage_attempted_epoch: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    """Last outage epoch whose one refunded resume this ticket already used."""

    first_reported_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    """When heartbeat ingest first confirmed this lease against a live slot.

    NULL means this lease has never once been observed running, which is a
    different claim from "it is not running now" and must never be read as the
    latter. A validator can hold a lease for many minutes before its first
    progress report -- pulling the screened image, rendering the dataset, or
    seeding, which alone is allowed 15 minutes for v7 against a 5-minute
    reporting grace -- and during that window the slot is simply absent from
    ``capacity.active``, indistinguishable from an idle one.

    So this is the evidence :func:`~ditto.db.queries.lease_liveness.lease_liveness`
    needs to separate "has been quiet since it started" from "has gone quiet",
    and it is only ever used to REFUSE a revocation. Set once and never moved:
    the question it answers is whether this lease has *ever* testified, not when
    it last did. Reset to NULL on reissue, since a new lease has its own silence
    to account for.
    """

    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Latest signed public-safe failure class reported for this ticket."""

    failed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    """When :attr:`failure_reason` was reported. Preserved across reissue."""

    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The reporter's own code or note behind :attr:`failure_reason`, if any.

    :attr:`failure_reason` is a three-value class chosen to drive reissue policy,
    which makes it useless for diagnosis: ditto-subnet#279 traced twelve dead
    ``mnemo*`` leases to ``fail_job(reason="infrastructure")`` and still could not
    say *which* of the validator's five sandbox infrastructure codes fired,
    because the wire could not carry it and the code survived only in a
    validator-host log line. The ~60-minute killer is still unidentified for that
    reason alone. This is where that code now lands.

    Advisory and validator-supplied: it is not part of the signed fail payload
    (neither is :attr:`failure_reason`), it drives no policy, and its length is
    bounded by ``FailJobRequest`` alone -- see
    ``ditto.api_models.validator.FAILURE_DETAIL_MAX_LENGTH``. The column itself
    is ``TEXT`` and always has been, so widening that cap (200 -> 4096) needed no
    migration and no rewrite of existing rows: every value ever stored here is
    still valid, and the only thing that changed is how much a validator is
    allowed to send. Written and cleared together with :attr:`failure_reason`, so
    the pair is always read as one report. NULL means the reporter sent none --
    which is what every validator predating the field does, and is not an error.

    A value ending in ``...[truncated, N chars]`` is an amputated message, not a
    whole one: the sender hit the cap and said so. Before that marker existed a
    truncation was silent, and the half-sentence it left behind read as a
    complete finding -- which is how the 2026-07-27 inference-decline diagnosis
    lost the clause that named what the platform had actually done.
    """

    container_log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The failing harness's own bounded, redacted stdout/stderr tail.

    Where :attr:`failure_detail` carries the code an operator groups by, this
    carries what they read. The scorer has captured it on every failed sandbox
    run since the container-log-tail change -- already tailed to 2000 bytes with
    injected credentials masked and URL query strings stripped -- but no wire
    carried it, so it reached a human only by shelling into whichever validator
    host ran the job, against an in-memory store that had usually dropped it.
    Agent ``5fdadd33`` burned four leases in 82-108 seconds each and left four
    validators, the miner and an operator with nothing behind ``scoring_error``.
    That is what this column ends.

    Deliberately its own column rather than more :attr:`failure_detail`. That
    field is the one thing here an operator can GROUP BY; folding 2 KB of
    free-form output into it would turn a machine-readable code back into prose,
    which is the exact regression the scorer refuses to make on its own side.

    Advisory, unsigned, and bounded by ``FailJobRequest`` alone -- see
    ``ditto.api_models.validator.CONTAINER_LOG_TAIL_MAX_LENGTH``. Written and
    cleared together with :attr:`failure_reason`, so the report is always read as
    one. NULL means the reporter sent none: every validator predating the field,
    every failure with no container behind it, and every container that produced
    no output. That is not an error.

    **Untrusted.** Unlike every other column on this table, this is miner-authored
    output verbatim -- it can contain their source via a stack trace, arbitrary
    control bytes, or text written to manipulate whoever reads it. Render it as
    data, never as instructions, and never parse it for machine meaning.
    """

    container_log_tail_attempt: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    """:attr:`attempt_count` of the lease that wrote :attr:`container_log_tail`.

    This row is mutable: reissue restamps :attr:`issued_at` and increments
    :attr:`attempt_count` while retaining the prior failure fields. Without this
    stamp a later lease, or a later success, would look like it produced the
    previous attempt's tail. NULL means no tail was reported.
    """

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When this row was first inserted (UTC)."""

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    """When this row was last updated (UTC)."""

    __table_args__ = (
        PrimaryKeyConstraint(
            "agent_id",
            "bench_version",
            "validator_hotkey",
            name="validator_tickets_pkey",
        ),
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="validator_tickets_agent_id_fkey",
        ),
        Index("validator_tickets_agent_id_idx", "agent_id"),
        CheckConstraint(
            "bench_version > 0",
            name="validator_tickets_bench_version_positive",
        ),
        CheckConstraint(
            "purpose IN ("
            "'legacy_unclassified', 'canonical_quorum', 'continual_retest'"
            ")",
            name="validator_tickets_purpose_valid",
        ),
        CheckConstraint(
            "purpose_revision >= 0",
            name="validator_tickets_purpose_revision_nonnegative",
        ),
        CheckConstraint(
            "attempt_count > 0",
            name="validator_tickets_attempt_count_positive",
        ),
        CheckConstraint(
            "manual_retry_grants >= 0",
            name="validator_tickets_manual_retry_grants_nonnegative",
        ),
        # Created by 2026_07_23_add_validator_ticket_datasets.py; production
        # has enforced it since. NULL is a ticket issued before per-ticket
        # dataset seeding, not a seed of zero.
        CheckConstraint(
            "seed IS NULL OR seed >= 0",
            name="validator_tickets_seed_nonnegative",
        ),
        CheckConstraint(
            "infra_retry_grants >= 0",
            name="validator_tickets_infra_retry_grants_nonnegative",
        ),
        CheckConstraint(
            "failure_reason IS NULL OR failure_reason IN "
            "('infrastructure', 'scoring_error', 'sandbox_oom')",
            name="validator_tickets_failure_reason",
        ),
        CheckConstraint(
            "slot_id IN ('slot-0', 'slot-1', 'slot-2', 'slot-3', "
            "'slot-4', 'slot-5', 'slot-6', 'slot-7')",
            name="validator_tickets_slot_id",
        ),
        # The expiry sweep and the live-slot count both scan open tickets only;
        # a partial index keeps those hot paths off the full table.
        Index(
            "validator_tickets_open_idx",
            "deadline",
            postgresql_where=text("status = 'issued'"),
        ),
        Index(
            "validator_tickets_one_issued_per_validator_slot_idx",
            "validator_hotkey",
            "slot_id",
            unique=True,
            postgresql_where=text("status = 'issued'"),
            sqlite_where=text("status = 'issued'"),
        ),
    )


class InferenceGrant(Base):
    """One ticket-scoped platform inference capability."""

    __tablename__ = "inference_grants"

    grant_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    slot_id: Mapped[str] = mapped_column(Text, nullable=False)
    ticket_deadline: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    bearer_digest: Mapped[str | None] = mapped_column(Text, nullable=True)
    broker_public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    allowed_models: Mapped[list[str]] = mapped_column(_JSON_VARIANT, nullable=False)
    route_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_profile: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_quantization: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_prompt_price_per_token: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    route_completion_price_per_token: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    request_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    token_budget: Mapped[int] = mapped_column(BigInteger, nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_profile: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_provider: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_request_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_token_budget: Mapped[int] = mapped_column(BigInteger, nullable=False)
    embedding_request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    embedding_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    embedding_cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    embedding_active_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    active_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    usage_accounting_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default=text("1")
    )
    """Which metering contract booked this grant's token counters.

    A token total is only comparable within the contract that produced it --
    the same axis as ``bench_version`` for scores, and for the same reason.

    ``1`` -- reservations were ``max_tokens + len(body)``, byte length used as a
    token count, roughly 4x the truth. Any request that did not return trusted
    provider usage (timeout, transport failure, revocation, or a stale sweep)
    was charged that inflated figure as ``prompt_tokens``.

    ``2`` -- reservations are a real token estimate, so a reclaimed request is
    charged approximately what it cost.

    Successful calls booked the provider's own numbers under both versions, so
    the two differ only on the unsettled tail. The marker exists anyway: without
    it, someone comparing a v1 run against a v2 run later concludes an agent got
    dramatically more efficient when only the meter changed. There is no
    backfill and there cannot be one -- the over-charge happened at reservation
    time, so what those calls really consumed was never recorded.
    """
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id", "bench_version", "validator_hotkey"],
            [
                "validator_tickets.agent_id",
                "validator_tickets.bench_version",
                "validator_tickets.validator_hotkey",
            ],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "agent_id",
            "bench_version",
            "validator_hotkey",
            "ticket_deadline",
            name="inference_grants_ticket_lease",
        ),
        CheckConstraint(
            "status IN ('pending', 'active', 'revoked', 'exhausted')",
            name="inference_grants_status",
        ),
        CheckConstraint("request_budget > 0", name="inference_grants_request_budget"),
        CheckConstraint("token_budget > 0", name="inference_grants_token_budget"),
        CheckConstraint(
            "embedding_request_budget > 0",
            name="inference_grants_embedding_request_budget",
        ),
        CheckConstraint(
            "embedding_token_budget > 0",
            name="inference_grants_embedding_token_budget",
        ),
        CheckConstraint(
            "embedding_dimensions = 768",
            name="inference_grants_embedding_dimensions",
        ),
        CheckConstraint(
            "embedding_active_requests >= 0",
            name="inference_grants_embedding_active_requests",
        ),
        CheckConstraint(
            "active_requests >= 0", name="inference_grants_active_requests"
        ),
        Index("inference_grants_expiry_idx", "expires_at"),
    )


class ProviderOutageCircuit(Base):
    """Relay-owned provider-wide backpressure state consumed by Platform."""

    __tablename__ = "provider_outage_circuits"

    provider: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    epoch: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    retry_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    last_failure_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    failure_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
    last_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error_code: Mapped[str] = mapped_column(Text, nullable=False)
    probe_kind: Mapped[str | None] = mapped_column(Text, nullable=True)
    probe_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    probe_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('open', 'closed')",
            name="provider_outage_circuits_state_check",
        ),
        CheckConstraint(
            "failure_count > 0",
            name="provider_outage_circuits_failure_count_check",
        ),
        CheckConstraint(
            "last_status IS NULL OR last_status BETWEEN 400 AND 599",
            name="provider_outage_circuits_status_check",
        ),
        CheckConstraint(
            "(probe_kind IS NULL AND probe_key IS NULL "
            "AND probe_expires_at IS NULL) OR "
            "(probe_kind IN ('scoring', 'screening') "
            "AND probe_key IS NOT NULL AND probe_expires_at IS NOT NULL)",
            name="provider_outage_circuits_probe_check",
        ),
    )


class InferenceProviderRoute(Base):
    """Discovered OpenRouter route plus platform-observed serving quality."""

    __tablename__ = "inference_provider_routes"

    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    profile_revision: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="discovered")
    calibration_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="shadow"
    )
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantization: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_price_per_token: Mapped[float | None] = mapped_column(Float, nullable=True)
    completion_price_per_token: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    ewma_tokens_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    ewma_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ewma_error_rate: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    ewma_timeout_rate: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default=text("0")
    )
    ewma_tool_accuracy: Mapped[float | None] = mapped_column(Float, nullable=True)
    ewma_composite: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration_tool_accuracy: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )
    calibration_composite: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration_sample_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    calibration_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    calibration_manifest_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    calibrated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    ewma_cost_microusd: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    selected_ticket_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    exploration_ticket_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_selected_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    discovered_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    last_observed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint("model", "provider", "profile_revision"),
        UniqueConstraint("profile_revision", name="inference_provider_profile_key"),
        CheckConstraint(
            "status IN ('discovered', 'healthy', 'degraded', 'offline')",
            name="inference_provider_route_status",
        ),
        CheckConstraint(
            "calibration_status IN ('shadow', 'eligible', 'disabled')",
            name="inference_provider_calibration_status",
        ),
        CheckConstraint(
            "ewma_error_rate >= 0 AND ewma_error_rate <= 1",
            name="inference_provider_error_rate",
        ),
        CheckConstraint(
            "ewma_timeout_rate >= 0 AND ewma_timeout_rate <= 1",
            name="inference_provider_timeout_rate",
        ),
        CheckConstraint(
            "calibration_revision >= 0",
            name="inference_provider_calibration_revision",
        ),
        Index(
            "inference_provider_routes_selection_idx",
            "model",
            "calibration_status",
            "status",
        ),
    )


class InferenceRoutingPolicy(Base):
    """Audited operator-tunable policy for one benchmark model."""

    __tablename__ = "inference_routing_policies"

    model: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    speed_weight: Mapped[float] = mapped_column(Float, nullable=False)
    cost_weight: Mapped[float] = mapped_column(Float, nullable=False)
    exploration_weight: Mapped[float] = mapped_column(Float, nullable=False)
    exploration_ticket_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    min_tool_accuracy: Mapped[float] = mapped_column(Float, nullable=False)
    min_composite: Mapped[float] = mapped_column(Float, nullable=False)
    min_calibration_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    max_error_rate: Mapped[float] = mapped_column(Float, nullable=False)
    max_timeout_rate: Mapped[float] = mapped_column(Float, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    ewma_alpha: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "speed_weight >= 0 AND cost_weight >= 0 AND exploration_weight >= 0",
            name="inference_routing_policy_weights",
        ),
        CheckConstraint(
            "speed_weight + cost_weight + exploration_weight > 0",
            name="inference_routing_policy_nonzero_weights",
        ),
        CheckConstraint(
            "min_tool_accuracy >= 0 AND min_tool_accuracy <= 1 "
            "AND min_composite >= 0 AND min_composite <= 1",
            name="inference_routing_policy_quality",
        ),
        CheckConstraint(
            "max_error_rate >= 0 AND max_error_rate <= 1 "
            "AND max_timeout_rate >= 0 AND max_timeout_rate <= 1 "
            "AND ewma_alpha > 0 AND ewma_alpha <= 1",
            name="inference_routing_policy_reliability",
        ),
        CheckConstraint(
            "exploration_ticket_budget >= 0 AND min_calibration_samples > 0 "
            "AND cooldown_seconds >= 1 AND revision >= 0",
            name="inference_routing_policy_bounds",
        ),
    )


class InferenceRoutingAudit(Base):
    """Append-only operator history for inference policy and route decisions."""

    __tablename__ = "inference_routing_audit"

    audit_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    profile_revision: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (Index("inference_routing_audit_history_idx", "recorded_at"),)


class InferenceRequest(Base):
    """Replay ledger and bounded accounting for one proxy request."""

    __tablename__ = "inference_requests"

    grant_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="started")
    request_kind: Mapped[str] = mapped_column(Text, nullable=False, default="chat")
    model: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """Stored estimate of the call, for untrusted-receipt clamping only.

    Token spend is receipted provider usage. This figure is not admission-gated
    and is not booked onto the grant when a request never returns usage.
    """
    max_chargeable_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    """Hard ceiling on what untrusted provider accounting may claim.

    Byte-derived and therefore tokenizer-independent: a token cannot consume
    less than one byte of the UTF-8 request body. Split from ``reserved_tokens``
    when that became an estimate -- clamping real usage to an estimate would
    mark ordinary token-dense requests non-deliverable. Rows written before the
    split carry the old reservation here, which *was* this same bound.
    """
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    upstream_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    upstream_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    openrouter_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    fallback_phase: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    terminal_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        PrimaryKeyConstraint("grant_id", "nonce"),
        ForeignKeyConstraint(
            ["grant_id"], ["inference_grants.grant_id"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'canceled')",
            name="inference_requests_status",
        ),
        CheckConstraint(
            "request_kind IN ('chat', 'embedding')",
            name="inference_requests_kind",
        ),
        CheckConstraint(
            "reserved_tokens > 0", name="inference_requests_reserved_tokens"
        ),
        CheckConstraint("generation > 0", name="inference_requests_generation"),
        CheckConstraint(
            "upstream_attempts >= 0", name="inference_requests_upstream_attempts"
        ),
        CheckConstraint(
            "openrouter_attempts >= 0",
            name="inference_requests_openrouter_attempts",
        ),
        CheckConstraint(
            "fallback_phase BETWEEN 0 AND 1",
            name="inference_requests_fallback_phase",
        ),
        Index("inference_requests_started_idx", "started_at"),
        # In-flight rows only: the runtime-metrics stale count and the
        # admission path's live-request counts test ``status = 'started'``
        # against a ledger of tens of millions of settled rows. See the
        # 2026_08_21 migration for why this is partial and built concurrently.
        Index(
            "inference_requests_inflight_idx",
            "request_kind",
            "started_at",
            postgresql_where=text("status = 'started'"),
        ),
    )


class ValidatorLeaseAudit(Base):
    """Append-only record of every lease the platform revoked before its deadline.

    Force-expiring a live lease destroys an in-flight benchmark run and spends
    one of the agent's bounded same-version retries, and until this table existed
    it left no trace at all: no log line, no row, no metric, just a ticket whose
    ``deadline`` had been silently rewritten to the moment of the revocation. A
    day of investigation went into reconstructing three such events from ticket
    timestamps alone. Every revocation now records the evidence it acted on, so
    the next one is a query rather than an archaeology project.

    Not part of the score audit chain: no score, verdict, or miner-visible
    outcome is created or replaced here, only a lease lifecycle event.
    """

    __tablename__ = "validator_lease_audit"

    audit_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    slot_id: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    """What happened to the lease; ``force_expired`` today."""
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    """The liveness reason code that admitted the revocation."""
    context: Mapped[str] = mapped_column(Text, nullable=False)
    """Which issuance path revoked it (``issue_ticket``, ``issue_rollout_ticket``,
    ``score_retest``)."""
    evidence: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    """The heartbeat freshness, capacity snapshot, and lease age acted on."""
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "bench_version > 0",
            name="validator_lease_audit_bench_version_positive",
        ),
        Index(
            "validator_lease_audit_agent_idx",
            "agent_id",
            "recorded_at",
        ),
        Index(
            "validator_lease_audit_validator_idx",
            "validator_hotkey",
            "recorded_at",
        ),
    )


class ValidatorRetryRecovery(Base):
    """One immutable operator action that restores bounded validation eligibility.

    A recovery snapshots every ticket before changing only the selected expired
    rows' retry grant counters. It is separate from the public score audit log:
    no score or miner verdict is being created, replaced, or removed.
    """

    __tablename__ = "validator_retry_recoveries"

    recovery_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expected_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_snapshot: Mapped[list[dict]] = mapped_column(_JSON_VARIANT, nullable=False)
    granted_validator_hotkeys: Mapped[list[str]] = mapped_column(
        _JSON_VARIANT, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="validator_retry_recoveries_agent_id_fkey",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_retry_recoveries_actor_length",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 3",
            name="validator_retry_recoveries_reason_length",
        ),
        CheckConstraint(
            "score_count >= 0",
            name="validator_retry_recoveries_score_count_nonnegative",
        ),
        CheckConstraint(
            "bench_version > 0",
            name="validator_retry_recoveries_bench_version_positive",
        ),
        Index(
            "validator_retry_recoveries_agent_created_idx",
            "agent_id",
            "bench_version",
            "created_at",
            "recovery_id",
        ),
        UniqueConstraint(
            "agent_id",
            "bench_version",
            "expected_snapshot",
            name="validator_retry_recoveries_agent_snapshot_key",
        ),
    )


class ValidatorQueueWithdrawal(Base):
    """One audited, benchmark-scoped removal from validator assignment.

    The submission, payment, artifact, scores, and ticket history remain intact.
    A later benchmark era is a separate eligibility decision, so a withdrawal
    never becomes an implicit permanent miner verdict.

    Two operator routes write this row and both land in the same terminal state,
    which is why they share a table rather than duplicating the queue predicates
    that read it. They differ only in what they were allowed to act on, and
    :attr:`evicted_validator_hotkeys` is what tells them apart:

    * a **withdrawal** (``NULL``) cleans up a submission that had already stopped
      consuming validator capacity;
    * an **eviction** (a list, possibly empty) additionally revoked whatever live
      leases the submission was still holding, naming them.

    A row is *in force* only while :attr:`reinstated_at` is ``NULL``. Reversing
    an eviction resolves the row rather than deleting it — see
    :class:`ValidatorQueueReinstatement`.
    """

    __tablename__ = "validator_queue_withdrawals"

    withdrawal_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expected_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_snapshot: Mapped[list[dict]] = mapped_column(_JSON_VARIANT, nullable=False)
    evicted_validator_hotkeys: Mapped[list[str] | None] = mapped_column(
        _JSON_VARIANT, nullable=True
    )
    """The live leases this action revoked, or ``NULL`` for a plain withdrawal.

    Deliberately tri-state. ``NULL`` means the eviction route was not used at
    all; ``[]`` means it was, and found nothing live to revoke. Collapsing those
    into one value would make an eviction that arrived a minute too late
    indistinguishable from an ordinary cleanup, which is precisely the
    distinction an emissions-relevant action against a paying miner has to be
    able to defend later.
    """
    reinstated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    """When this removal was reversed, or ``NULL`` while it is still in force.

    The resolution pointer, deliberately *on the removal row*. Every queue
    predicate in the codebase already asks one question of this table — "is this
    agent removed from this era?" — and answering it after reinstatement has to
    stay a single indexed read rather than a correlated join against another
    table. The reinstatement's own actor, reason, and evidence live in
    :class:`ValidatorQueueReinstatement`; this column carries only the fact.
    """
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="validator_queue_withdrawals_agent_id_fkey",
        ),
        CheckConstraint(
            "bench_version > 0",
            name="validator_queue_withdrawals_bench_version_positive",
        ),
        CheckConstraint(
            "score_count >= 0",
            name="validator_queue_withdrawals_score_count_nonnegative",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_queue_withdrawals_actor_length",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="validator_queue_withdrawals_reason_length",
        ),
        # One removal *in force* per (agent, benchmark era). Partial rather than
        # a plain unique constraint because reinstatement is reversible in both
        # directions: an operator who reinstates a submission and then watches it
        # starve the fleet again must be able to evict it a second time in the
        # same era, and a total constraint would make the first reinstatement
        # permanently disarm the lever the whole feature exists to provide.
        # Resolved rows stay, so the era's full removal history is preserved.
        Index(
            "validator_queue_withdrawals_agent_bench_key",
            "agent_id",
            "bench_version",
            unique=True,
            postgresql_where=text("reinstated_at IS NULL"),
        ),
        Index(
            "validator_queue_withdrawals_created_idx",
            "created_at",
            "withdrawal_id",
        ),
    )


class ValidatorQueueReinstatement(Base):
    """One audited reversal of an operator eviction, returning it to the queue.

    Eviction was one-way until this existed, so an operator could only free a
    starved fleet by ending a paying miner's benchmark era, and the miner's only
    route back was a fresh submission and a second evaluation fee. That made a
    capacity lever into a punishment, and it was the sole reason the lever went
    unused against the ``mnemo*`` family, whose source review found self-imposed
    per-case deadlines, no hang primitives, and a version trajectory of pure
    latency reduction — costly, but not shown to be malicious.

    Deliberately its **own append-only row** rather than a mutation of
    :class:`ValidatorQueueWithdrawal`, and rather than deleting that row:

    * an eviction that was later reversed is a fact worth keeping. It named live
      validator leases it destroyed, and each of those has a
      ``validator_lease_audit`` row that ``GET /admin/lease-revocations
      ?action=operator_evicted`` reads; deleting the eviction would orphan them
      and quietly rewrite what the operator did on 2026-07-27;
    * the two actions have different actors, different reasons, and different
      idempotency keys. Overwriting ``actor``/``reason`` in place would destroy
      the evictor's justification to record the reinstator's.

    The reversal itself is deliberately *inert*: it restores eligibility and
    nothing else. It does not resurrect leases, reset ``attempt_count``, mint a
    no-fault grant, or clear the operator-recovery count. See
    :attr:`retry_budget_snapshot`.
    """

    __tablename__ = "validator_queue_reinstatements"

    reinstatement_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True
    )
    withdrawal_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expected_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_snapshot: Mapped[list[dict]] = mapped_column(_JSON_VARIANT, nullable=False)
    retry_budget_snapshot: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    """The attempt budget as it stood when the submission came back.

    Recorded because "unchanged" is the security property of this route and an
    assertion nobody can check later is not an assertion. A reinstatement grants
    no attempt and lifts no cap, so a submission returns with exactly the budget
    it was evicted with; the pair of numbers here (the agent's fleet-wide
    ``infra_retry_grants`` at this era, bounded by ``MAX_AGENT_INFRA_RETRY_GRANTS``,
    and its append-only operator-recovery count) is what lets a reviewer confirm
    that an evict/reinstate cycle laundered no attempt budget.
    """
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["withdrawal_id"],
            ["validator_queue_withdrawals.withdrawal_id"],
            ondelete="RESTRICT",
            name="validator_queue_reinstatements_withdrawal_id_fkey",
        ),
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="validator_queue_reinstatements_agent_id_fkey",
        ),
        CheckConstraint(
            "bench_version > 0",
            name="validator_queue_reinstatements_bench_version_positive",
        ),
        CheckConstraint(
            "score_count >= 0",
            name="validator_queue_reinstatements_score_count_nonnegative",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_queue_reinstatements_actor_length",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="validator_queue_reinstatements_reason_length",
        ),
        # One reversal per removal. The removal row is resolved exactly once, so
        # a second reinstatement of the same eviction is a database-level
        # impossibility rather than a route-level check that could be bypassed.
        UniqueConstraint(
            "withdrawal_id",
            name="validator_queue_reinstatements_withdrawal_key",
        ),
        Index(
            "validator_queue_reinstatements_agent_idx",
            "agent_id",
            "bench_version",
            "created_at",
        ),
    )


class SubmissionRetirement(Base):
    """One audited close-out of a submission whose benchmark era has ended.

    Retirement is a *generational* fact, not a verdict. It records that the
    benchmark version this submission was queued against is no longer the
    active one, so no validator will ever issue it another ticket: the era
    closed underneath it. The payment, artifact, screening record, accepted
    scores and ticket history are all preserved, and the row stays visible and
    searchable on the submissions page.

    Deliberately a sibling of :class:`ValidatorQueueWithdrawal` rather than a
    reuse of it. The two answer different questions and a reader must be able to
    tell them apart:

    * a **withdrawal** says the submission burned its slots and can no longer
      reach quorum on its own; it is scoped to a benchmark version because a
      later era is a fresh eligibility decision.
    * a **retirement** says the submission never got the chance, because the
      subnet moved to a newer benchmark generation. It names the version that
      closed (``bench_version``) and the version that superseded it
      (``superseded_by_version``), so the miner-facing copy can say which
      generation their work belonged to.

    Retirement is also deliberately *not* an :class:`AgentStatus` value. That
    enum is the screener/platform wire contract shipped by
    ``ditto-screening-protocol``, and :data:`SCOREABLE_AGENT_STATUSES` feeds the
    score ledger. Introducing a status would change scoring semantics for a
    display problem. The agent row keeps ``status = evaluating``; this table is
    what the queue predicates and the public projection read.
    """

    __tablename__ = "submission_retirements"

    retirement_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    superseded_by_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expected_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_snapshot: Mapped[list[dict]] = mapped_column(_JSON_VARIANT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="submission_retirements_agent_id_fkey",
        ),
        CheckConstraint(
            "bench_version > 0",
            name="submission_retirements_bench_version_positive",
        ),
        CheckConstraint(
            "superseded_by_version > bench_version",
            name="submission_retirements_superseded_by_is_newer",
        ),
        CheckConstraint(
            "score_count >= 0",
            name="submission_retirements_score_count_nonnegative",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="submission_retirements_actor_length",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="submission_retirements_reason_length",
        ),
        UniqueConstraint(
            "agent_id",
            "bench_version",
            name="submission_retirements_agent_bench_key",
        ),
        Index(
            "submission_retirements_created_idx",
            "created_at",
            "retirement_id",
        ),
    )


class ValidatorRequestNonce(Base):
    """A recently consumed signed validator request nonce.

    Rows are short-lived replay guards. The UUID primary key makes consuming a
    nonce atomic across every platform replica; a bounded singleton janitor
    prunes expired rows outside signed request transactions.
    """

    __tablename__ = "validator_request_nonces"

    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (Index("validator_request_nonces_expires_at_idx", "expires_at"),)


class BannedHotkey(Base):
    """One row of the ``banned_hotkeys`` table.

    A hotkey-level ban, distinct from the per-agent :attr:`AgentStatus.BANNED`
    status: it blocks the *miner* (all future uploads) rather than a single
    submission. Enforced at upload (``/upload/agent`` rejects a banned hotkey
    before any expensive chain/payment work) and surfaced on the read path
    (``/retrieval/agent-by-hotkey``). Created out-of-band by the owner via
    ``scripts/ban_hotkey.py``. Backroom can inspect active rows and remove one
    exact gate through the authenticated admin API; there is no public write
    surface.
    """

    __tablename__ = "banned_hotkeys"

    hotkey: Mapped[str] = mapped_column(Text, primary_key=True)
    """SS58 hotkey of the banned miner. Primary key (a hotkey is banned once)."""

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Free-text audit note (e.g. "confirmed copy of agent <id>"). Nullable."""

    banned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """When the ban was recorded (UTC)."""


class HotkeyBanAudit(Base):
    """Append-only audit of operator changes to hotkey-level bans.

    The active upload gate remains the compact ``banned_hotkeys`` table.  This
    ledger preserves the row an operator removed, together with the signed-in
    Backroom identity and public-safe reason, so an unban never erases its own
    evidence.
    """

    __tablename__ = "hotkey_ban_audit"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_banned_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("action = 'unban'", name="hotkey_ban_audit_action"),
        Index("hotkey_ban_audit_hotkey_recorded_idx", "hotkey", "recorded_at"),
    )


class ScoreAuditEntry(Base):
    """One append-only, hash-chained entry in the public score audit log.

    Every scoring *event* (a validator recording a score, and an agent
    finalizing at the k=3 median) appends one immutable row here, in the same
    transaction as the score write. Unlike ``scores`` (which is UPSERTed per
    ``(agent, validator)`` and so reflects only the *current* score), this table
    is never updated or deleted — it is the durable, ordered history.

    Tamper-evidence is a hash chain: ``entry_hash`` = SHA-256 over the entry's
    canonical JSON (which embeds ``prev_hash``), and ``prev_hash`` is the
    previous entry's ``entry_hash`` (genesis = 64 zeros). Editing or removing any
    historical entry breaks every subsequent link, so a public consumer that
    replays the chain can prove nothing was silently rewritten. Each ``score``
    entry also carries the validator's sr25519 ``signature`` verbatim, so an
    entry is independently authenticatable against the published validator key —
    the log adds ordering + immutability on top of the already-signed payload.
    """

    __tablename__ = "score_audit_log"

    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    """Monotonic append order (BIGSERIAL on Postgres, INTEGER rowid on SQLite)."""

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """Agent the event is about. Not FK-bound — the log outlives the agent row
    (agents may be pruned; the audit history must not cascade away)."""

    validator_hotkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Reporting validator for a ``score`` event; null for an ``agent_finalized``
    event (which is the platform's own median computation, not one validator's)."""

    event: Mapped[str] = mapped_column(Text, nullable=False)
    """Event kind: ``score`` (one validator's signed score) or ``agent_finalized``
    (quorum reached; the median + participating validators)."""

    payload: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    """The event's immutable content, JSON. For ``score``: the full signed tuple
    (run_id, seed, composite, tool/memory means, median_ms, n, signature,
    generated_at). For ``agent_finalized``: median_composite, quorum, the
    scoring validators, and the pinned dataset. Hashed into ``entry_hash``."""

    prev_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """The previous entry's ``entry_hash`` (hex); ``"0" * 64`` for the genesis."""

    entry_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """SHA-256 (hex) over this entry's canonical JSON, which embeds ``prev_hash``.
    The chain link a public verifier recomputes to detect tampering."""

    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    """When the platform appended the entry (UTC). Part of the hashed content."""

    __table_args__ = (
        UniqueConstraint("entry_hash", name="score_audit_log_entry_hash_key"),
        Index("score_audit_log_agent_id_idx", "agent_id"),
    )


class EfficiencyBonusSettingsRevision(Base):
    """Append-only, operator-audited relative-efficiency-bonus policy revision.

    The hot-swappable successor to the ``DITTO_EFFICIENCY_BONUS_*`` boot env
    (:class:`ditto.api_server.config.EfficiencyBonusConfig`): each row freezes a
    full policy (both booleans + all eight numeric knobs) as JSON, and the
    compute path reads the latest one at run time (short TTL) so a backroom
    change lands with no redeploy. Rows are **append-only**: nothing UPDATEs or
    deletes one, so the audit trail is complete. When no row exists the env seed
    governs, so an untouched deployment is byte-identical to pre-change.

    Scope is subnet-global (only ``*``); the column mirrors the other revision
    tables' shape and leaves room for future per-board scoping. The frozen knobs
    that reproduce a published bonus live on ``efficiency_cohort_snapshots``, not
    here, so changing this policy never mutates an already-frozen snapshot.
    """

    __tablename__ = "efficiency_bonus_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "scope = '*' OR length(scope) BETWEEN 1 AND 63",
            name="efficiency_bonus_settings_scope_check",
        ),
        CheckConstraint(
            "length(checksum) = 64",
            name="efficiency_bonus_settings_checksum_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="efficiency_bonus_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="efficiency_bonus_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="efficiency_bonus_settings_actor_check",
        ),
        Index(
            "efficiency_bonus_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "scope",
            "parent_revision",
            name="efficiency_bonus_settings_scope_parent_key",
        ),
    )


class ContinualRetestSettingsRevision(Base):
    """Append-only operator policy for continual aggregation and idle waves."""

    __tablename__ = "continual_retest_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("scope = '*'", name="continual_retest_settings_scope_check"),
        CheckConstraint(
            "length(checksum) = 64",
            name="continual_retest_settings_checksum_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="continual_retest_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="continual_retest_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="continual_retest_settings_actor_check",
        ),
        Index(
            "continual_retest_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "scope",
            "parent_revision",
            name="continual_retest_settings_scope_parent_key",
        ),
    )


class BurnSettingsRevision(Base):
    """Append-only operator policy for the subnet-owner emission burn.

    One scalar (``burn_share``) that the scoring ledger serves to every
    validator, replacing a constant that previously required a validator release
    to move. Append-only for the same reason every other emission-affecting
    control here is: the question after an unexpected weight vector is always
    "what was the policy at the time", and only a revision log answers it.
    """

    __tablename__ = "burn_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("scope = '*'", name="burn_settings_scope_check"),
        CheckConstraint(
            "length(checksum) = 64",
            name="burn_settings_checksum_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="burn_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="burn_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="burn_settings_actor_check",
        ),
        Index(
            "burn_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "scope",
            "parent_revision",
            name="burn_settings_scope_parent_key",
        ),
    )


class QueuePolicySettingsRevision(Base):
    """Append-only operator policy for the validator queue.

    Cohort sizing, the per-validator fresh-vs-cohort lane split, the provisional
    contender lane, and the previous-generation carryover gate. Each revision
    stores the WHOLE policy rather than a diff, so any historical revision is
    reconstructable on its own and a read never merges partial revisions.

    Fields are consumed on three different schedules -- frozen at rollout start,
    live-but-rollout-locked, and plain live. See
    ``ditto.api_models.queue_policy_settings`` for which is which and why it
    matters.
    """

    __tablename__ = "queue_policy_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("scope = '*'", name="queue_policy_settings_scope_check"),
        CheckConstraint(
            "length(checksum) = 64",
            name="queue_policy_settings_checksum_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="queue_policy_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="queue_policy_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="queue_policy_settings_actor_check",
        ),
        Index(
            "queue_policy_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "scope",
            "parent_revision",
            name="queue_policy_settings_scope_parent_key",
        ),
    )


class ScreenerPolicyActivation(Base):
    """Append-only schedule for raising the required screening-policy version.

    Screening policy text ships with the deployed Platform/worker build (the
    build implements every version up to ``SCREENING_POLICY_VERSION``), but the
    version the queue REQUIRES is a subnet decision with a fairness timeline:
    miners must get equal notice that the rules are changing, so the newest
    text activates on a schedule instead of at deploy time. Until a scheduled
    activation is due, the platform requires ``SCREENING_FLOOR_POLICY_VERSION``
    and dual-text workers screen under that older version; when ``activate_at``
    passes, the required version rises to ``target_policy_version`` and every
    agent screened under a stale version re-enters the screening queue on the
    same criteria.

    Each revision stores the whole schedule decision (never a diff), so the
    operator audit trail is complete and immutable. The newest revision whose
    ``activate_at`` has passed governs; a newer not-yet-due revision never
    pulls the required version down.
    """

    __tablename__ = "screener_policy_activations"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    activate_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    rescreen_scored: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "target_policy_version >= 1",
            name="screener_policy_activation_target_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="screener_policy_activation_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="screener_policy_activation_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="screener_policy_activation_actor_check",
        ),
        UniqueConstraint(
            "parent_revision",
            name="screener_policy_activation_parent_key",
        ),
    )


class ScoredScreeningSnapshotRestoration(Base):
    """Append-only audit for restoring a scored row displaced by a rescreen.

    The displaced attempt remains untouched. This row records the exact prior
    successful attempt whose policy attestation was restored, plus the score
    quorum observed while the agent row was locked.
    """

    __tablename__ = "scored_screening_snapshot_restorations"

    restoration_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    displaced_attempt_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), nullable=False
    )
    restored_attempt_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), nullable=False
    )
    source_activation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    current_activation_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_status: Mapped[str] = mapped_column(Text, nullable=False)
    previous_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    restored_policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    score_count: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="scored_screening_restorations_agent_id_fkey",
        ),
        ForeignKeyConstraint(
            ["displaced_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
            name="scored_screening_restorations_displaced_attempt_fkey",
        ),
        ForeignKeyConstraint(
            ["restored_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
            name="scored_screening_restorations_restored_attempt_fkey",
        ),
        CheckConstraint(
            "source_policy_version > target_policy_version",
            name="scored_screening_restorations_policy_order_check",
        ),
        CheckConstraint(
            "restored_policy_version <= target_policy_version",
            name="scored_screening_restorations_restored_policy_check",
        ),
        CheckConstraint(
            "bench_version > 0 AND score_count >= 3",
            name="scored_screening_restorations_score_check",
        ),
        CheckConstraint(
            "previous_status IN ('screening_failed', 'rejected')",
            name="scored_screening_restorations_previous_status_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="scored_screening_restorations_actor_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="scored_screening_restorations_reason_check",
        ),
        UniqueConstraint(
            "displaced_attempt_id",
            name="scored_screening_restorations_displaced_attempt_key",
        ),
        Index("scored_screening_restorations_batch_idx", "batch_id", "created_at"),
    )


class InferenceConcurrencySettingsRevision(Base):
    """Append-only operator policy for hosted inference admission.

    Governs chat budgets plus how many hosted chat and embedding requests may be
    in flight per ticket, per validator, and fleet-wide. The local-Ollama lane
    used by bench_version 2-6 is not reachable from this table.

    Each revision stores the WHOLE policy rather than a diff, so any historical
    revision is reconstructable on its own and a read never merges partials. See
    ``ditto.api_models.inference_concurrency_settings`` for the bounds and why
    the shipped defaults sit above what the validator will ever ask for.
    """

    __tablename__ = "inference_concurrency_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "scope = '*'", name="inference_concurrency_settings_scope_check"
        ),
        CheckConstraint(
            "length(checksum) = 64",
            name="inference_concurrency_settings_checksum_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="inference_concurrency_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="inference_concurrency_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="inference_concurrency_settings_actor_check",
        ),
        Index(
            "inference_concurrency_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "scope",
            "parent_revision",
            name="inference_concurrency_settings_scope_parent_key",
        ),
    )


class ValidatorSlotSettingsRevision(Base):
    """Append-only, operator-audited concurrent-benchmark-slot policy revision.

    A validator advertises its own slot capacity in the heartbeat (bounded to
    eight by the protocol) and the platform used to honor it unconditionally.
    Each row here freezes a full policy as JSON -- the per-validator cap plus the
    disk-utilization circuit breaker -- and ticket dispatch reads the latest one
    at run time (short TTL) so an operator can cap or ramp fleet parallelism from
    backroom with no redeploy. Rows are **append-only**: nothing UPDATEs or
    deletes one, so the audit trail of every cap change is complete.

    When no row exists the module default in
    ``ditto.api_server.validator_slot_settings`` governs (cap 2), which is also
    the fallback for every failure path -- an unreadable policy holds the fleet
    conservative rather than uncapping it.

    Scope is subnet-global (only ``*``); the column mirrors the other revision
    tables' shape and leaves room for future per-validator scoping.
    """

    __tablename__ = "validator_slot_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "scope = '*' OR length(scope) BETWEEN 1 AND 63",
            name="validator_slot_settings_scope_check",
        ),
        CheckConstraint(
            "length(checksum) = 64",
            name="validator_slot_settings_checksum_check",
        ),
        CheckConstraint(
            "parent_revision >= 0",
            name="validator_slot_settings_parent_revision_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8",
            name="validator_slot_settings_reason_check",
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="validator_slot_settings_actor_check",
        ),
        Index(
            "validator_slot_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        # The optimistic-concurrency key: two writers that both read revision N
        # as current cannot both append a child of N.
        UniqueConstraint(
            "scope",
            "parent_revision",
            name="validator_slot_settings_scope_parent_key",
        ),
    )


class ArtifactFetchAudit(Base):
    """One durable record of a served artifact — bytes or a presigned URL.

    Every path that hands a caller miner source (the tarball, a source file, a
    source diff) or a presigned URL to it appends one row here. Before this
    table the only trace of an artifact fetch was a stdout line, so an artifact
    leak could be observed in its consequences and never attributed to a
    fetcher. This is the forensic record that answers "who fetched this
    artifact, and when".

    Insert-only and *not* FK-bound to ``agents``: the record must outlive the
    submission it describes, exactly as :class:`ScoreAuditEntry` does. An agent
    row may be pruned; the history of who read its source must not cascade away.

    Deliberately *not* hash-chained. :class:`ScoreAuditEntry` serializes every
    append on a ``FOR UPDATE`` head lock to keep one linear chain; that is the
    right trade for a public tamper-evident score projection, and the wrong one
    for a read-path audit, where it would turn concurrent artifact fetches into
    a queue behind a single row. This table takes plain concurrent INSERTs.

    ``requester_kind`` is a closed vocabulary; ``requester_id`` is that kind's
    identity (validator hotkey, screener hotkey, admin actor) and is NULL only
    for the unauthenticated public path, where no identity exists to record.

    Distinct from :class:`ValidatorLeaseAudit`, which records lease *revocations*
    — a destructive write the platform performs on a validator. This records
    *reads* a caller performs against miner source. They share neither a
    requester model (that table is validator-only and requires a hotkey, a slot
    and a bench version on every row; fetches also arrive from an
    identity-less public route and a shared-hotkey screener fleet) nor a query
    shape, so they stay separate tables.

    One row per *fetch*, never updated. ``validator_tickets`` is keyed by
    ``(agent_id, bench_version, validator_hotkey)`` and is UPSERTed on reissue,
    so a repeated claim/fail loop collapses into one row with an inflated
    ``attempt_count`` and only the latest ``issued_at``. Because a fetch appends
    here instead, that same loop leaves one durable row per artifact hand-off —
    the per-lease history the ticket table cannot keep.
    """

    __tablename__ = "artifact_fetch_audit"

    seq: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    """Monotonic append order (BIGSERIAL on Postgres, INTEGER rowid on SQLite)."""

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """Agent whose artifact was served. Not FK-bound — see the class docstring."""

    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    """Stable identifier of the serving route, e.g. ``validator.agent_artifact``.

    A route *name*, not the request path: paths carry ids and change shape, and
    the question this column answers is "which door did they come through"."""

    requester_kind: Mapped[str] = mapped_column(Text, nullable=False)
    """``validator`` | ``screener`` | ``admin`` | ``public``."""

    requester_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Hotkey (validator/screener) or admin actor. NULL only for ``public``."""

    requester_instance_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Screener worker instance within the shared-hotkey fleet, when known.

    ``SCREENER_HOTKEY`` is one shared string across an autoscaled fleet, so the
    hotkey alone cannot say *which* worker read the source. NULL means the
    caller did not identify its instance — an attribution gap, not an error."""

    lease_id: Mapped[UUID | None] = mapped_column(SaUUID(as_uuid=True), nullable=True)
    """The claim this fetch was bound to: a screening ``attempt_id`` today.

    Validator leases have no surrogate id — ``validator_tickets`` is keyed by
    ``(agent_id, validator_hotkey)``, both already recorded above — so the
    ticket is identified by that pair plus :attr:`bench_version`."""

    bench_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """Benchmark era of the authorizing validator ticket, when there was one."""

    artifact_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Digest of the artifact served, so a leaked copy can be matched to a fetch."""

    source_ip: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Peer address when the ASGI transport exposed one; NULL when it did not."""

    detail: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    """Route-specific extras (the source path read, the diff side, ...)."""

    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    """When the artifact was served (UTC)."""

    __table_args__ = (
        CheckConstraint(
            "requester_kind IN ('validator', 'screener', 'admin', 'public')",
            name="artifact_fetch_audit_requester_kind_check",
        ),
        CheckConstraint(
            "(requester_kind = 'public') = (requester_id IS NULL)",
            name="artifact_fetch_audit_requester_id_presence_check",
        ),
        CheckConstraint(
            "bench_version IS NULL OR bench_version > 0",
            name="artifact_fetch_audit_bench_version_check",
        ),
        # "Who fetched this agent's artifact, newest first" — the question asked
        # first when a specific submission is suspected of having leaked.
        Index(
            "artifact_fetch_audit_agent_idx",
            "agent_id",
            "fetched_at",
        ),
        # "Everything this requester ever fetched" — the pivot from a suspected
        # agent to the full reach of whoever fetched it.
        Index(
            "artifact_fetch_audit_requester_idx",
            "requester_kind",
            "requester_id",
            "fetched_at",
        ),
        # Bare time-range sweeps ("everything fetched during the leak window").
        Index(
            "artifact_fetch_audit_fetched_idx",
            "fetched_at",
            "seq",
        ),
    )


class OwnerAttestation(Base):
    """One cryptographically-proven owner link.

    Rows live on the ``owner_attestations`` table.

    Two hotkeys declared to be the same operator, with **both** endpoints
    having signed. Each half is proved by either that hotkey itself or the
    coldkey bound to it by payment records; the key kind is recorded per side
    so a reviewer can grade the evidence. Signatures are retained verbatim so
    any reviewer -- or any miner disputing a hold -- can re-verify the claim
    offline without trusting this table.

    The pair is stored in canonical (sorted) order as ``hotkey_lo`` /
    ``hotkey_hi``. The link is **symmetric**: it says one operator holds both
    keys, and both key holders consented. Symmetry is safe precisely because
    both halves are signed -- an attacker cannot mint an edge naming a hotkey
    they do not control, in either direction. ``lo``/``hi`` carry no meaning
    beyond giving the pair exactly one representation.

    Active links are authoritative same-operator evidence for copy screening,
    queue-capacity serialization, and one-slot-per-owner emission
    partitioning. The score ledger retains each underlying submission while
    selecting only the linked owner's best eligible row for emissions.
    """

    __tablename__ = "owner_attestations"

    attestation_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    """Surrogate key."""

    netuid: Mapped[int] = mapped_column(Integer, nullable=False)
    """Subnet the attestation was minted for. Signed, so it cannot be replayed
    onto another subnet running this software."""

    hotkey_lo: Mapped[str] = mapped_column(Text, nullable=False)
    """Lexicographically smaller of the two linked hotkeys."""

    hotkey_hi: Mapped[str] = mapped_column(Text, nullable=False)
    """Lexicographically larger of the two linked hotkeys."""

    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    """Single-use value bound into both signed payloads. Uniquely indexed: this
    is the replay guard, so a captured attestation cannot be submitted twice."""

    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    """Miner-supplied mint time, signed. Bounds the mint-to-submit window."""

    lo_key_kind: Mapped[str] = mapped_column(Text, nullable=False)
    """``hotkey`` or ``coldkey`` -- which key proved the ``lo`` half."""

    lo_signer: Mapped[str] = mapped_column(Text, nullable=False)
    """SS58 that actually signed the ``lo`` half. Equals ``hotkey_lo`` for a
    hotkey proof, or the payment-bound coldkey for a coldkey proof."""

    lo_signature: Mapped[str] = mapped_column(Text, nullable=False)
    """sr25519 signature for the ``lo`` half, lowercase hex, 128 chars."""

    hi_key_kind: Mapped[str] = mapped_column(Text, nullable=False)
    """``hotkey`` or ``coldkey`` -- which key proved the ``hi`` half."""

    hi_signer: Mapped[str] = mapped_column(Text, nullable=False)
    """SS58 that actually signed the ``hi`` half."""

    hi_signature: Mapped[str] = mapped_column(Text, nullable=False)
    """sr25519 signature for the ``hi`` half, lowercase hex, 128 chars."""

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """Row insertion time. The authoritative "link became active" instant --
    ``issued_at`` is miner-supplied and only loosely trusted."""

    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    """When the link stopped counting, or ``NULL`` while active.

    Revocation is **prospective**. Submissions already screened under an active
    link keep their decision: screening is a point-in-time adjudication, and
    re-holding already-scored agents on a later revocation would punish
    reliance and destabilise the ledger. The row is never deleted, so the
    window during which the link was live stays auditable.
    """

    revoked_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Who revoked: an operator actor, or the endpoint that withdrew."""

    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Free-text rationale recorded at revocation."""

    __table_args__ = (
        UniqueConstraint("nonce", name="owner_attestations_nonce_key"),
        CheckConstraint(
            "hotkey_lo < hotkey_hi",
            name="owner_attestations_canonical_order",
        ),
        CheckConstraint(
            "lo_key_kind IN ('hotkey', 'coldkey')",
            name="owner_attestations_lo_key_kind_check",
        ),
        CheckConstraint(
            "hi_key_kind IN ('hotkey', 'coldkey')",
            name="owner_attestations_hi_key_kind_check",
        ),
        CheckConstraint(
            "length(lo_signature) = 128",
            name="owner_attestations_lo_signature_check",
        ),
        CheckConstraint(
            "length(hi_signature) = 128",
            name="owner_attestations_hi_signature_check",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name="owner_attestations_revocation_pair",
        ),
        # At most one *active* link per pair. Canonical ordering means this
        # constrains the unordered pair, not one arrangement of it. A revoked
        # link can be re-established with a fresh nonce.
        Index(
            "owner_attestations_active_pair_idx",
            "netuid",
            "hotkey_lo",
            "hotkey_hi",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # The screening-time resolution reads both columns; the link is
        # symmetric so neither side is privileged.
        Index("owner_attestations_lo_idx", "netuid", "hotkey_lo"),
        Index("owner_attestations_hi_idx", "netuid", "hotkey_hi"),
    )


class NameClaim(Base):
    """One signed reservation of a public miner handle.

    Rows live on the ``name_claims`` table.

    A miner signs a claim over a normalized name stem (``jupiter`` covers
    ``Jupiter-ditto-v10``). Other miners who already have a scored
    payment-owner family endorse it. Once the endorsement threshold is
    met the stem is reserved to the claimant's owner family: new uploads
    that collide are rejected, and existing collisions are marked
    disputed on the public board.

    This table is **not** an input to emission-slot allocation. The
    reservation is a public-name control only.
    """

    __tablename__ = "name_claims"

    claim_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    netuid: Mapped[int] = mapped_column(Integer, nullable=False)
    name_stem: Mapped[str] = mapped_column(Text, nullable=False)
    claimant_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    claimant_key_kind: Mapped[str] = mapped_column(Text, nullable=False)
    claimant_signer: Mapped[str] = mapped_column(Text, nullable=False)
    claimant_signature: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    upheld_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    withdrawn_signer: Mapped[str | None] = mapped_column(Text, nullable=True)
    withdrawn_signature: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("nonce", name="name_claims_nonce_key"),
        CheckConstraint(
            "status IN ('pending', 'upheld', 'withdrawn')",
            name="name_claims_status_check",
        ),
        CheckConstraint(
            "claimant_key_kind IN ('hotkey', 'coldkey')",
            name="name_claims_key_kind_check",
        ),
        CheckConstraint(
            "length(claimant_signature) = 128",
            name="name_claims_signature_check",
        ),
        CheckConstraint(
            "length(name_stem) BETWEEN 3 AND 64",
            name="name_claims_stem_length_check",
        ),
        CheckConstraint(
            "name_stem ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="name_claims_stem_charset_check",
        ),
        CheckConstraint(
            "(status = 'upheld') = (upheld_at IS NOT NULL)",
            name="name_claims_upheld_pair",
        ),
        CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL)",
            name="name_claims_withdrawn_pair",
        ),
        Index(
            "name_claims_active_stem_idx",
            "netuid",
            "name_stem",
            unique=True,
            postgresql_where=text("status IN ('pending', 'upheld')"),
        ),
        Index("name_claims_status_idx", "netuid", "status"),
        Index("name_claims_claimant_idx", "netuid", "claimant_hotkey"),
    )


class NameClaimEndorsement(Base):
    """One entrenched-miner endorsement of a handle claim."""

    __tablename__ = "name_claim_endorsements"

    endorsement_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    claim_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    endorser_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    endorser_owner_root: Mapped[str] = mapped_column(Text, nullable=False)
    endorser_key_kind: Mapped[str] = mapped_column(Text, nullable=False)
    endorser_signer: Mapped[str] = mapped_column(Text, nullable=False)
    endorser_signature: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["claim_id"],
            ["name_claims.claim_id"],
            ondelete="CASCADE",
            name="name_claim_endorsements_claim_id_fkey",
        ),
        UniqueConstraint("nonce", name="name_claim_endorsements_nonce_key"),
        UniqueConstraint(
            "claim_id",
            "endorser_owner_root",
            name="name_claim_endorsements_owner_key",
        ),
        CheckConstraint(
            "endorser_key_kind IN ('hotkey', 'coldkey')",
            name="name_claim_endorsements_key_kind_check",
        ),
        CheckConstraint(
            "length(endorser_signature) = 128",
            name="name_claim_endorsements_signature_check",
        ),
        Index(
            "name_claim_endorsements_claim_idx",
            "claim_id",
            "created_at",
        ),
    )


class MinerAvatar(Base):
    """One current profile picture per miner hotkey."""

    __tablename__ = "miner_avatars"

    miner_hotkey: Mapped[str] = mapped_column(Text, primary_key=True)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("nonce", name="miner_avatars_nonce_key"),
        CheckConstraint("length(sha256) = 64", name="miner_avatars_sha256_check"),
        CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name="miner_avatars_content_type_check",
        ),
    )


class MinerAvatarNonce(Base):
    """Replay log for signed avatar set/clear nonces."""

    __tablename__ = "miner_avatar_nonces"

    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class MinerProfile(Base):
    """Optional public miner profile fields (socials). Identity is the hotkey."""

    __tablename__ = "miner_profiles"

    miner_hotkey: Mapped[str] = mapped_column(Text, primary_key=True)
    x_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    discord_handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "x_url IS NULL OR length(x_url) BETWEEN 8 AND 200",
            name="miner_profiles_x_url_len",
        ),
        CheckConstraint(
            "github_url IS NULL OR length(github_url) BETWEEN 8 AND 200",
            name="miner_profiles_github_url_len",
        ),
        CheckConstraint(
            "discord_handle IS NULL OR length(discord_handle) BETWEEN 2 AND 32",
            name="miner_profiles_discord_len",
        ),
        CheckConstraint(
            "discord_handle IS NULL OR discord_handle ~ '^[A-Za-z0-9._]{2,32}$'",
            name="miner_profiles_discord_charset",
        ),
    )


class MinerSession(Base):
    """One signed-in miner capability grant (dashboard, MCP, or CLI)."""

    __tablename__ = "miner_sessions"

    session_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "label IN ('dashboard', 'mcp', 'cli')",
            name="miner_sessions_label_check",
        ),
        CheckConstraint(
            "length(scopes) BETWEEN 1 AND 200",
            name="miner_sessions_scopes_len",
        ),
        CheckConstraint(
            "length(miner_hotkey) BETWEEN 47 AND 48",
            name="miner_sessions_hotkey_len",
        ),
        Index("miner_sessions_hotkey_idx", "miner_hotkey", "expires_at"),
    )


class MinerSessionToken(Base):
    """One issued bearer for a miner session. Hash only; never the raw token."""

    __tablename__ = "miner_session_tokens"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["session_id"],
            ["miner_sessions.session_id"],
            ondelete="CASCADE",
            name="miner_session_tokens_session_id_fkey",
        ),
        CheckConstraint(
            "length(token_hash) = 64",
            name="miner_session_tokens_hash_len",
        ),
        Index("miner_session_tokens_session_idx", "session_id"),
    )


class MinerOauthClient(Base):
    """Dynamically registered public MCP OAuth client (PKCE, no secret)."""

    __tablename__ = "miner_oauth_clients"

    client_id: Mapped[str] = mapped_column(Text, primary_key=True)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    redirect_uris: Mapped[list[str]] = mapped_column(_JSON_VARIANT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "length(client_id) BETWEEN 16 AND 80",
            name="miner_oauth_clients_id_len",
        ),
        CheckConstraint(
            "length(client_name) BETWEEN 1 AND 120",
            name="miner_oauth_clients_name_len",
        ),
    )


class MinerDeviceGrant(Base):
    """One device-authorization / MCP consent grant awaiting a hotkey signature."""

    __tablename__ = "miner_device_grants"

    grant_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    user_code: Mapped[str] = mapped_column(Text, nullable=False)
    poll_token_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(Text, nullable=False)
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    miner_hotkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_id: Mapped[UUID | None] = mapped_column(SaUUID(as_uuid=True), nullable=True)
    oauth_client_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    state: Mapped[str | None] = mapped_column(Text, nullable=True)
    code_challenge: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("user_code", name="miner_device_grants_user_code_key"),
        UniqueConstraint("poll_token_hash", name="miner_device_grants_poll_token_key"),
        ForeignKeyConstraint(
            ["session_id"],
            ["miner_sessions.session_id"],
            ondelete="SET NULL",
            name="miner_device_grants_session_id_fkey",
        ),
        ForeignKeyConstraint(
            ["oauth_client_id"],
            ["miner_oauth_clients.client_id"],
            ondelete="SET NULL",
            name="miner_device_grants_client_id_fkey",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'expired', 'denied', 'consumed')",
            name="miner_device_grants_status_check",
        ),
        CheckConstraint(
            "user_code ~ '^[A-Z0-9]{4}-[A-Z0-9]{4}$'",
            name="miner_device_grants_user_code_fmt",
        ),
        CheckConstraint(
            "ttl_seconds BETWEEN 3600 AND 2592000",
            name="miner_device_grants_ttl_range",
        ),
        CheckConstraint(
            "poll_token_hash IS NULL OR length(poll_token_hash) = 64",
            name="miner_device_grants_poll_hash_len",
        ),
        Index("miner_device_grants_status_idx", "status", "expires_at"),
    )


class MinerOauthCode(Base):
    """One-time OAuth authorization code that exchanges for a session token."""

    __tablename__ = "miner_oauth_codes"

    code_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    grant_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    session_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["grant_id"],
            ["miner_device_grants.grant_id"],
            ondelete="CASCADE",
            name="miner_oauth_codes_grant_id_fkey",
        ),
        ForeignKeyConstraint(
            ["session_id"],
            ["miner_sessions.session_id"],
            ondelete="CASCADE",
            name="miner_oauth_codes_session_id_fkey",
        ),
        CheckConstraint(
            "length(code_hash) = 64",
            name="miner_oauth_codes_hash_len",
        ),
    )


class MinerLoginNonce(Base):
    """Replay log for signed miner login approvals."""

    __tablename__ = "miner_login_nonces"

    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    miner_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    used_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class ConfirmationBundleSettingsRevision(Base):
    """Append-only global policy for the bounded v9 confirmation lane."""

    __tablename__ = "confirmation_bundle_settings_revisions"

    revision: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    settings: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("scope = '*'", name="confirmation_settings_scope_check"),
        CheckConstraint(
            "length(checksum) = 64", name="confirmation_settings_checksum_check"
        ),
        CheckConstraint(
            "parent_revision >= 0", name="confirmation_settings_parent_check"
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8", name="confirmation_settings_reason_check"
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="confirmation_settings_actor_check",
        ),
        Index(
            "confirmation_settings_scope_revision_idx",
            "scope",
            "revision",
            unique=True,
        ),
        UniqueConstraint(
            "scope", "parent_revision", name="confirmation_settings_parent_key"
        ),
        UniqueConstraint(
            "revision", "checksum", name="confirmation_settings_revision_checksum_key"
        ),
    )


class ConfirmationRetestAuthorization(Base):
    """Append-only operator authority for one new immutable bundle generation."""

    __tablename__ = "confirmation_retest_authorizations"

    authorization_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True
    )
    source_bundle_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_revision: Mapped[str] = mapped_column(Text, nullable=False)
    profile_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    from_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    authorized_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_retest_source_bundle_fkey",
        ),
        CheckConstraint(
            "length(artifact_sha256) = 64", name="confirmation_retest_sha_check"
        ),
        CheckConstraint("bench_version >= 9", name="confirmation_retest_version_check"),
        CheckConstraint(
            "length(profile_revision) BETWEEN 1 AND 128",
            name="confirmation_retest_profile_revision_check",
        ),
        CheckConstraint(
            "length(profile_checksum) = 64",
            name="confirmation_retest_profile_checksum_check",
        ),
        CheckConstraint(
            "from_generation >= 0 AND authorized_generation = from_generation + 1",
            name="confirmation_retest_generation_check",
        ),
        CheckConstraint(
            "length(trim(reason)) >= 8", name="confirmation_retest_reason_check"
        ),
        CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="confirmation_retest_actor_check",
        ),
        UniqueConstraint(
            "artifact_sha256",
            "bench_version",
            "profile_revision",
            "profile_checksum",
            "authorized_generation",
            name="confirmation_retest_generation_key",
        ),
        UniqueConstraint(
            "authorization_id",
            "artifact_sha256",
            "bench_version",
            "profile_revision",
            "profile_checksum",
            "authorized_generation",
            name="confirmation_retest_bundle_fkey_target",
        ),
    )


class ConfirmationBundle(Base):
    """One digest/profile/generation-deduplicated expensive v9 bundle."""

    __tablename__ = "confirmation_bundles"

    bundle_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_revision: Mapped[str] = mapped_column(Text, nullable=False)
    profile_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    retest_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retest_authorization_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    generation_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="initial"
    )
    source_bundle_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_checksum: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    qualification_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    completion_ticket_id: Mapped[UUID | None] = mapped_column(
        SaUUID(as_uuid=True), nullable=True
    )
    evidence_root: Mapped[dict | None] = mapped_column(_JSON_VARIANT, nullable=True)
    evidence_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    reporter_hotkey: Mapped[str | None] = mapped_column(Text, nullable=True)
    bundle_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["settings_revision", "settings_checksum"],
            [
                "confirmation_bundle_settings_revisions.revision",
                "confirmation_bundle_settings_revisions.checksum",
            ],
            name="confirmation_bundles_settings_fkey",
        ),
        ForeignKeyConstraint(
            ["completion_ticket_id", "bundle_id"],
            [
                "confirmation_bundle_tickets.ticket_id",
                "confirmation_bundle_tickets.bundle_id",
            ],
            name="confirmation_bundles_completion_ticket_fkey",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            [
                "retest_authorization_id",
                "artifact_sha256",
                "bench_version",
                "profile_revision",
                "profile_checksum",
                "retest_generation",
            ],
            [
                "confirmation_retest_authorizations.authorization_id",
                "confirmation_retest_authorizations.artifact_sha256",
                "confirmation_retest_authorizations.bench_version",
                "confirmation_retest_authorizations.profile_revision",
                "confirmation_retest_authorizations.profile_checksum",
                "confirmation_retest_authorizations.authorized_generation",
            ],
            name="confirmation_bundles_retest_auth_fkey",
        ),
        ForeignKeyConstraint(
            ["source_bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_bundles_source_fkey",
        ),
        CheckConstraint(
            "length(artifact_sha256) = 64", name="confirmation_bundles_sha_check"
        ),
        CheckConstraint(
            "bench_version >= 9", name="confirmation_bundles_version_check"
        ),
        CheckConstraint(
            "length(profile_revision) BETWEEN 1 AND 128",
            name="confirmation_bundles_profile_revision_check",
        ),
        CheckConstraint(
            "length(profile_checksum) = 64",
            name="confirmation_bundles_profile_checksum_check",
        ),
        CheckConstraint(
            "length(settings_checksum) = 64",
            name="confirmation_bundles_settings_checksum_check",
        ),
        CheckConstraint(
            "(generation_reason = 'initial' AND retest_generation = 0 "
            "AND retest_authorization_id IS NULL AND source_bundle_id IS NULL) OR "
            "(generation_reason = 'operator_retest' AND retest_generation > 0 "
            "AND retest_authorization_id IS NOT NULL "
            "AND source_bundle_id IS NOT NULL) OR "
            "(generation_reason = 'settings_supersession' "
            "AND retest_generation > 0 AND retest_authorization_id IS NULL "
            "AND source_bundle_id IS NOT NULL)",
            name="confirmation_bundles_generation_auth_check",
        ),
        CheckConstraint(
            "state IN ('blocked_budget', 'pending', 'leased', 'failed', "
            "'completed', 'superseded')",
            name="confirmation_bundles_state_check",
        ),
        CheckConstraint(
            "(state NOT IN ('completed', 'superseded') "
            "AND qualification_status IS NULL AND completion_mode IS NULL "
            "AND completion_ticket_id IS NULL AND evidence_root IS NULL "
            "AND evidence_sha256 IS NULL AND reporter_hotkey IS NULL "
            "AND bundle_signature IS NULL AND verified_at IS NULL "
            "AND completed_at IS NULL) OR "
            "(state IN ('completed', 'superseded') "
            "AND qualification_status IN ('qualified', 'unqualified') "
            "AND completion_mode IN ('shadow', 'enforce') "
            "AND completion_ticket_id IS NOT NULL AND evidence_root IS NOT NULL "
            "AND length(evidence_sha256) = 64 "
            "AND length(trim(reporter_hotkey)) > 0 "
            "AND length(bundle_signature) BETWEEN 2 AND 512 "
            "AND verified_at IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(state = 'superseded' AND qualification_status IS NULL "
            "AND completion_mode IS NULL AND completion_ticket_id IS NULL "
            "AND evidence_root IS NULL AND evidence_sha256 IS NULL "
            "AND reporter_hotkey IS NULL AND bundle_signature IS NULL "
            "AND verified_at IS NULL AND completed_at IS NULL)",
            name="confirmation_bundles_completion_check",
        ),
        UniqueConstraint(
            "artifact_sha256",
            "bench_version",
            "profile_revision",
            "profile_checksum",
            "retest_generation",
            name="confirmation_bundles_identity_key",
        ),
        UniqueConstraint(
            "retest_authorization_id", name="confirmation_bundles_retest_auth_key"
        ),
        UniqueConstraint("source_bundle_id", name="confirmation_bundles_source_key"),
        Index("confirmation_bundles_state_created_idx", "state", "created_at"),
    )


class ConfirmationBundleSubject(Base):
    """Per-agent base/full contract state, separate from shared evidence."""

    __tablename__ = "confirmation_bundle_subjects"

    agent_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    bench_version: Mapped[int] = mapped_column(Integer, nullable=False)
    artifact_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_id: Mapped[UUID | None] = mapped_column(SaUUID(as_uuid=True), nullable=True)
    result_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="base_only"
    )
    base_evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    base_quality_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    base_stderr_micros: Mapped[int] = mapped_column(Integer, nullable=False)
    base_model_factor_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    base_tool_factor_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    full_quality_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    full_stderr_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    semantic_factor_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied_factor_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    full_effective_micros: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "agent_id", "bench_version", name="confirmation_bundle_subjects_pkey"
        ),
        ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="confirmation_subjects_agent_fkey",
        ),
        ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_subjects_bundle_fkey",
        ),
        CheckConstraint(
            "bench_version >= 9", name="confirmation_subjects_version_check"
        ),
        CheckConstraint(
            "length(artifact_sha256) = 64", name="confirmation_subjects_sha_check"
        ),
        CheckConstraint(
            "length(base_evidence_sha256) = 64",
            name="confirmation_subjects_base_evidence_sha_check",
        ),
        CheckConstraint(
            "base_quality_micros BETWEEN 0 AND 1000000 "
            "AND base_stderr_micros BETWEEN 0 AND 1000000",
            name="confirmation_subjects_base_score_check",
        ),
        CheckConstraint(
            "base_model_factor_bps IN (0, 10000) "
            "AND base_tool_factor_bps IN (0, 10000)",
            name="confirmation_subjects_base_factor_check",
        ),
        CheckConstraint(
            "(full_quality_micros IS NULL AND full_stderr_micros IS NULL "
            "AND semantic_factor_bps IS NULL AND applied_factor_bps IS NULL "
            "AND full_effective_micros IS NULL) OR "
            "(full_quality_micros BETWEEN 0 AND 1000000 "
            "AND full_stderr_micros BETWEEN 0 AND 1000000 "
            "AND semantic_factor_bps IN (0, 10000) "
            "AND applied_factor_bps IN (0, 10000) "
            "AND full_effective_micros BETWEEN 0 AND 1000000)",
            name="confirmation_subjects_full_projection_check",
        ),
        CheckConstraint(
            "(result_status = 'base_only' AND bundle_id IS NULL "
            "AND full_quality_micros IS NULL) OR "
            "(result_status = 'provisional' AND bundle_id IS NOT NULL) OR "
            "(result_status = 'full_confirmed' AND bundle_id IS NOT NULL "
            "AND full_quality_micros IS NOT NULL)",
            name="confirmation_subjects_status_bundle_check",
        ),
        Index("confirmation_subjects_bundle_idx", "bundle_id"),
        Index("confirmation_subjects_status_idx", "result_status"),
    )


class ConfirmationBundleTicket(Base):
    """Append-only four-hour lease attempt for one confirmation bundle."""

    __tablename__ = "confirmation_bundle_tickets"

    ticket_id: Mapped[UUID] = mapped_column(
        SaUUID(as_uuid=True), primary_key=True, default=uuid4
    )
    bundle_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    slot_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="issued")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    deadline: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Signed, allowlisted diagnostic class behind :attr:`failure_reason`.

    ``failure_reason`` is a four-value protocol class chosen to drive reissue
    policy; it cannot distinguish a sandbox OOM from an evidence-schema break.
    This is the smallest thing that localizes that boundary fleet-wide without
    becoming an error-string channel, and it is bound into the reporter's
    signature. Optional: a reporter predating the contract sends neither field.
    """

    failure_stage: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Last progress stage the slot published before it broke, or ``unknown``."""

    failed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    prepare_rejection: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Allowlisted Go→Python ``/prepare-report`` rejection.

    Distinct from :attr:`failure_class`, which is bound into the reporter
    fail-job signature after the validator already treated the 409 as
    ``platform`` / ``finalizing``. Null for historical rows and for tickets
    whose prepare convert/rebuild never ran or succeeded.
    """

    prepare_rejected_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_tickets_bundle_fkey",
        ),
        CheckConstraint(
            "status IN ('issued', 'scored', 'expired')",
            name="confirmation_tickets_status_check",
        ),
        CheckConstraint("attempt > 0", name="confirmation_tickets_attempt_check"),
        CheckConstraint(
            "deadline > issued_at", name="confirmation_tickets_deadline_check"
        ),
        CheckConstraint(
            "(status = 'expired') OR (failure_reason IS NULL AND failed_at IS NULL)",
            name="confirmation_tickets_failure_state_check",
        ),
        CheckConstraint(
            "(failure_reason IS NULL) = (failed_at IS NULL)",
            name="confirmation_tickets_failure_pair_check",
        ),
        CheckConstraint(
            "(failure_class IS NULL) = (failure_stage IS NULL)",
            name="confirmation_tickets_failure_diagnostic_pair_check",
        ),
        CheckConstraint(
            "failure_class IS NULL OR failure_reason IS NOT NULL",
            name="confirmation_tickets_failure_diagnostic_requires_reason_check",
        ),
        CheckConstraint(
            "failure_class IS NULL OR failure_class IN ("
            "'sandbox_oom', 'lease_revoked', 'validator_infrastructure', "
            "'platform_infrastructure', 'dittobench', 'platform', 'validator', "
            "'evidence_schema', 'timeout', 'transport', 'unclassified')",
            name="confirmation_tickets_failure_class_check",
        ),
        CheckConstraint(
            "failure_stage IS NULL OR failure_stage IN ("
            "'preparing', 'running_confirmation', 'finalizing', "
            "'submitting_result', 'failed_retrying', 'unknown')",
            name="confirmation_tickets_failure_stage_check",
        ),
        CheckConstraint(
            "(prepare_rejection IS NULL) = (prepare_rejected_at IS NULL)",
            name="confirmation_tickets_prepare_rejection_pair_check",
        ),
        CheckConstraint(
            "prepare_rejection IS NULL OR prepare_rejection IN ("
            "'go_evidence_digest_mismatch', 'go_evidence_fields_drifted', "
            "'unsupported_ablation_status', 'unsupported_ablation_contract', "
            "'ablation_profile_drift', 'ablation_accounting', "
            "'ablation_digest_mismatch', 'longmem_profile_drift', "
            "'longmem_accounting', 'longmem_digest_mismatch', "
            "'longmem_latency_drift', 'unsupported_bench_version', "
            "'confirmation_wire', 'confirmation_evidence', 'unclassified')",
            name="confirmation_tickets_prepare_rejection_check",
        ),
        UniqueConstraint(
            "bundle_id", "attempt", name="confirmation_tickets_attempt_key"
        ),
        UniqueConstraint(
            "ticket_id", "bundle_id", name="confirmation_tickets_id_bundle_key"
        ),
        Index(
            "confirmation_tickets_one_live_bundle_idx",
            "bundle_id",
            unique=True,
            postgresql_where=text("status = 'issued'"),
        ),
        Index(
            "confirmation_tickets_open_deadline_idx",
            "deadline",
            postgresql_where=text("status = 'issued'"),
        ),
    )


class ConfirmationInferenceGrant(Base):
    """One purpose-bound provider capability for a confirmation ticket.

    Confirmation deliberately has its own ledger instead of weakening the hot
    ordinary ``inference_grants`` table with nullable foreign keys.  The lane is
    part of the durable identity: a reader bearer can never authorize the
    official judge or the embedding service.
    """

    __tablename__ = "confirmation_inference_grants"

    grant_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    ticket_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    bundle_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    validator_hotkey: Mapped[str] = mapped_column(Text, nullable=False)
    lane: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    bearer_digest: Mapped[str] = mapped_column(Text, nullable=False)
    broker_public_key: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    route_provider: Mapped[str] = mapped_column(Text, nullable=False)
    receipt_provider: Mapped[str] = mapped_column(Text, nullable=False)
    profile_revision: Mapped[str] = mapped_column(Text, nullable=False)
    request_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    token_budget: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cost_budget_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    request_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    active_requests: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["ticket_id", "bundle_id"],
            [
                "confirmation_bundle_tickets.ticket_id",
                "confirmation_bundle_tickets.bundle_id",
            ],
            ondelete="CASCADE",
            name="confirmation_inference_grants_ticket_fkey",
        ),
        UniqueConstraint(
            "ticket_id", "lane", name="confirmation_inference_grants_ticket_lane_key"
        ),
        CheckConstraint(
            "lane IN ('reader', 'judge', 'embedding')",
            name="confirmation_inference_grants_lane_check",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked', 'exhausted')",
            name="confirmation_inference_grants_status_check",
        ),
        CheckConstraint(
            "request_budget > 0 AND token_budget > 0 AND cost_budget_microusd > 0",
            name="confirmation_inference_grants_budget_check",
        ),
        CheckConstraint(
            "request_count >= 0 AND prompt_tokens >= 0 "
            "AND completion_tokens >= 0 AND cost_microusd >= 0 "
            "AND active_requests >= 0",
            name="confirmation_inference_grants_accounting_check",
        ),
        CheckConstraint(
            "generation > 0", name="confirmation_inference_grants_generation_check"
        ),
        Index("confirmation_inference_grants_expiry_idx", "expires_at"),
    )


class ConfirmationInferenceRequest(Base):
    """Replay-safe accounting for one confirmation provider request."""

    __tablename__ = "confirmation_inference_requests"

    grant_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    nonce: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="started")
    model: Mapped[str] = mapped_column(Text, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_chargeable_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    completion_tokens: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    cost_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    upstream_provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        PrimaryKeyConstraint("grant_id", "nonce"),
        ForeignKeyConstraint(
            ["grant_id"],
            ["confirmation_inference_grants.grant_id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('started', 'completed', 'failed', 'canceled')",
            name="confirmation_inference_requests_status_check",
        ),
        CheckConstraint(
            "generation > 0 AND reserved_tokens > 0 "
            "AND max_chargeable_tokens >= reserved_tokens",
            name="confirmation_inference_requests_reservation_check",
        ),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND cost_microusd >= 0",
            name="confirmation_inference_requests_accounting_check",
        ),
        Index("confirmation_inference_requests_started_idx", "started_at"),
    )


class ConfirmationDimensionEvidence(Base):
    """Immutable typed evidence envelope for one shared bundle dimension."""

    __tablename__ = "confirmation_dimension_evidence"

    bundle_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    dimension: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provider_cost_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict] = mapped_column(_JSON_VARIANT, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        PrimaryKeyConstraint(
            "bundle_id", "dimension", name="confirmation_dimension_evidence_pkey"
        ),
        ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_evidence_bundle_fkey",
        ),
        CheckConstraint(
            "dimension IN ('longmemeval', 'inference_ablation', 'embedding_ablation')",
            name="confirmation_evidence_dimension_check",
        ),
        CheckConstraint(
            "status IN ('completed', 'not_run', 'unavailable')",
            name="confirmation_evidence_status_check",
        ),
        CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
            "AND provider_cost_microusd >= 0 AND latency_ms >= 0",
            name="confirmation_evidence_nonnegative_check",
        ),
        CheckConstraint(
            "(dimension = 'longmemeval' AND status = 'completed' "
            "AND synthetic = false) OR "
            "(dimension IN ('inference_ablation', 'embedding_ablation') "
            "AND synthetic = true AND request_count = 0 AND input_tokens = 0 "
            "AND output_tokens = 0 AND provider_cost_microusd = 0)",
            name="confirmation_evidence_synthetic_cost_check",
        ),
        CheckConstraint(
            "length(evidence_sha256) = 64",
            name="confirmation_evidence_sha_check",
        ),
    )


class ConfirmationBudgetDay(Base):
    """CAS-serialized accounting for new issuance on one UTC day."""

    __tablename__ = "confirmation_budget_days"

    utc_day: Mapped[date] = mapped_column(Date, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issued_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outstanding_reserved_microusd: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    settled_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "revision >= 0 AND issued_attempts >= 0 "
            "AND outstanding_reserved_microusd >= 0 AND settled_microusd >= 0",
            name="confirmation_budget_nonnegative_check",
        ),
    )


class ConfirmationBudgetReservation(Base):
    """One immutable issuance charge, settled against its original UTC day."""

    __tablename__ = "confirmation_budget_reservations"

    reservation_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), primary_key=True)
    bundle_id: Mapped[UUID] = mapped_column(SaUUID(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    utc_day: Mapped[date] = mapped_column(Date, nullable=False)
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_microusd: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="reserved")
    actual_microusd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failed_attempt: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_reservations_bundle_fkey",
        ),
        ForeignKeyConstraint(
            ["utc_day"],
            ["confirmation_budget_days.utc_day"],
            name="confirmation_reservations_day_fkey",
        ),
        ForeignKeyConstraint(
            ["settings_revision"],
            ["confirmation_bundle_settings_revisions.revision"],
            name="confirmation_reservations_settings_fkey",
        ),
        CheckConstraint(
            "attempt > 0 AND reserved_microusd > 0",
            name="confirmation_reservations_positive_check",
        ),
        CheckConstraint(
            "state IN ('reserved', 'settled')",
            name="confirmation_reservations_state_check",
        ),
        CheckConstraint(
            "(state = 'reserved' AND actual_microusd IS NULL "
            "AND failed_attempt IS NULL AND settled_at IS NULL) OR "
            "(state = 'settled' AND actual_microusd >= 0 "
            "AND failed_attempt IS NOT NULL AND settled_at IS NOT NULL)",
            name="confirmation_reservations_settlement_check",
        ),
        UniqueConstraint(
            "bundle_id", "attempt", name="confirmation_reservations_attempt_key"
        ),
        Index(
            "confirmation_reservations_one_open_bundle_idx",
            "bundle_id",
            unique=True,
            postgresql_where=text("state = 'reserved'"),
        ),
        Index("confirmation_reservations_day_idx", "utc_day"),
    )


class ValidatorNameCache(Base):
    """Last successful public validator metadata fetched by Platform."""

    __tablename__ = "validator_name_cache"

    validator_hotkey: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    stake_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    refreshed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "display_name IS NULL OR length(display_name) BETWEEN 1 AND 80",
            name="validator_name_cache_display_name_check",
        ),
        CheckConstraint(
            "stake_weight IS NULL OR stake_weight >= 0",
            name="validator_name_cache_stake_weight_check",
        ),
    )
