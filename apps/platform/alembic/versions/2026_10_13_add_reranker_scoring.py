"""add Season 2 reranker scoring lease and shadow champion history (SN-209)

Two changes, both for the trusted reranker scorer worker
(``ditto.reranker_scoring_worker``), which is off by default:

1. ``reranker_submissions`` gains a ``scoring`` status and a lease: an opaque
   ``scoring_token``, ``scoring_lease_expires_at`` and ``scoring_attempts``. A
   worker commits ``accepted -> scoring`` before any slow work; a crashed
   worker's lease expires and another worker reclaims it (bounded attempts).
   ``failure_reason`` holds a short allowlisted code for ``failed`` rows, and
   ``seed_block`` records the finalized block number beside
   ``seed_block_hash`` so anyone can re-derive the scored seed.
2. ``reranker_champions`` is the append-only shadow crown history. At most one
   row has ``superseded_at IS NULL`` (the current champion). No row means the
   pinned incumbent still holds the crown. A trigger rejects DELETE and any
   UPDATE other than stamping ``superseded_at`` once.

Shadow only: neither table is read by the memory track, validator allocation,
weights, emissions or payments.

Revision ID: f8af34f45dea
Revises: 3e8b5c1d9a47
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f8af34f45dea"
down_revision: str | Sequence[str] | None = "3e8b5c1d9a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST = "~ '^[0-9a-f]{64}$'"
_OLD_STATUSES = "'accepted', 'invalid', 'scored', 'failed'"
_NEW_STATUSES = "'accepted', 'scoring', 'invalid', 'scored', 'failed'"


def _replace_status_check(statuses: str) -> None:
    # Raw SQL: the full constraint name is what the metadata naming convention
    # rendered in the parent revision, and alembic must not re-prefix it.
    op.execute(
        "ALTER TABLE reranker_submissions "
        "DROP CONSTRAINT ck_reranker_submissions_status"
    )
    op.execute(
        "ALTER TABLE reranker_submissions ADD CONSTRAINT "
        f"ck_reranker_submissions_status CHECK (status IN ({statuses}))"
    )


def upgrade() -> None:
    # reranker_submissions is a new, small, cold table (shadow intake only), so
    # plain ADD COLUMN is fine; none of the hot-table helpers apply.
    _replace_status_check(_NEW_STATUSES)
    op.add_column(
        "reranker_submissions",
        sa.Column("scoring_token", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "reranker_submissions",
        sa.Column(
            "scoring_lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "reranker_submissions",
        sa.Column(
            "scoring_attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "reranker_submissions",
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "reranker_submissions",
        sa.Column("seed_block", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "scoring_lease",
        "reranker_submissions",
        "(status = 'scoring') = "
        "(scoring_token IS NOT NULL AND scoring_lease_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "scoring_attempts", "reranker_submissions", "scoring_attempts >= 0"
    )
    op.create_check_constraint(
        "seed_block",
        "reranker_submissions",
        "seed_block IS NULL OR (seed_block >= 0 AND seed_block_hash IS NOT NULL)",
    )
    op.create_check_constraint(
        "failure_reason",
        "reranker_submissions",
        "failure_reason IS NULL OR length(failure_reason) BETWEEN 1 AND 200",
    )

    op.create_table(
        "reranker_champions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        # NULL: the crown was taken from the pinned incumbent.
        sa.Column(
            "previous_submission_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "crowned_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("contract_sha256", sa.Text(), nullable=False),
        sa.Column("pools_sha256", sa.Text(), nullable=False),
        sa.Column("comparison", postgresql.JSONB(), nullable=False),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"contract_sha256 {_DIGEST} AND pools_sha256 {_DIGEST}", name="digests"
        ),
        sa.CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= crowned_at",
            name="superseded_after_crowned",
        ),
        sa.CheckConstraint(
            "previous_submission_id IS DISTINCT FROM submission_id",
            name="not_self",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["reranker_submissions.id"],
            name="reranker_champions_submission_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["previous_submission_id"],
            ["reranker_submissions.id"],
            name="reranker_champions_previous_submission_id_fkey",
        ),
        sa.PrimaryKeyConstraint("id", name="reranker_champions_pkey"),
        sa.UniqueConstraint(
            "submission_id", name="reranker_champions_submission_id_key"
        ),
    )
    # At most one current champion.
    op.create_index(
        "reranker_champions_one_current_idx",
        "reranker_champions",
        [sa.text("(superseded_at IS NULL)")],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "reranker_champions_crowned_idx", "reranker_champions", ["crowned_at"]
    )
    op.execute(
        """
        CREATE FUNCTION reranker_champions_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'reranker_champions is append-only';
            END IF;
            IF OLD.superseded_at IS NOT NULL
               OR NEW.superseded_at IS NULL
               OR NEW.id IS DISTINCT FROM OLD.id
               OR NEW.submission_id IS DISTINCT FROM OLD.submission_id
               OR NEW.previous_submission_id
                    IS DISTINCT FROM OLD.previous_submission_id
               OR NEW.crowned_at IS DISTINCT FROM OLD.crowned_at
               OR NEW.contract_sha256 IS DISTINCT FROM OLD.contract_sha256
               OR NEW.pools_sha256 IS DISTINCT FROM OLD.pools_sha256
               OR NEW.comparison IS DISTINCT FROM OLD.comparison THEN
                RAISE EXCEPTION
                    'reranker_champions rows only gain superseded_at, once';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER reranker_champions_append_only "
        "BEFORE UPDATE OR DELETE ON reranker_champions "
        "FOR EACH ROW EXECUTE FUNCTION reranker_champions_append_only()"
    )


def downgrade() -> None:
    bind = op.get_bind()
    crowned = bind.execute(sa.text("SELECT count(*) FROM reranker_champions")).scalar()
    if crowned:
        # The crown history is append-only evidence; dropping it is not a
        # rollback, it is data loss. Export and remove it deliberately first.
        raise RuntimeError(
            "refusing to downgrade: reranker_champions holds crown history"
        )
    op.execute("DROP TRIGGER reranker_champions_append_only ON reranker_champions")
    op.execute("DROP FUNCTION reranker_champions_append_only()")
    op.drop_index("reranker_champions_crowned_idx", table_name="reranker_champions")
    op.drop_index("reranker_champions_one_current_idx", table_name="reranker_champions")
    op.drop_table("reranker_champions")

    # An in-flight lease returns to the queue; the old schema has no 'scoring'.
    op.execute(
        "UPDATE reranker_submissions SET status = 'accepted', scoring_token = NULL, "
        "scoring_lease_expires_at = NULL WHERE status = 'scoring'"
    )
    for name in ("seed_block", "failure_reason", "scoring_attempts", "scoring_lease"):
        op.execute(
            "ALTER TABLE reranker_submissions "
            f"DROP CONSTRAINT ck_reranker_submissions_{name}"
        )
    op.drop_column("reranker_submissions", "seed_block")
    op.drop_column("reranker_submissions", "failure_reason")
    op.drop_column("reranker_submissions", "scoring_attempts")
    op.drop_column("reranker_submissions", "scoring_lease_expires_at")
    op.drop_column("reranker_submissions", "scoring_token")
    _replace_status_check(_OLD_STATUSES)
