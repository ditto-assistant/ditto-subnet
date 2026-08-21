"""add immutable shadow coding authoring freezes

Revision ID: c6d9f2a14b83
Revises: e4c7a1d9b260
Create Date: 2026-08-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c6d9f2a14b83"
down_revision: str | Sequence[str] | None = "e4c7a1d9b260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coding_shadow_authoring_freezes",
        sa.Column("freeze_id", sa.UUID(), nullable=False),
        sa.Column("ticket_id", sa.UUID(), nullable=False),
        sa.Column("run_row_id", sa.UUID(), nullable=False),
        sa.Column("task_count", sa.Integer(), nullable=False),
        sa.Column("authoring_evidence_sha256", sa.Text(), nullable=False),
        sa.Column("authoring_event_root", sa.Text(), nullable=False),
        sa.Column("authoring_transcript_sha256", sa.Text(), nullable=False),
        sa.Column("authoring_transcript_object_key", sa.Text(), nullable=False),
        sa.Column("authoring_transcript_bytes", sa.BigInteger(), nullable=False),
        sa.Column("authoring_event_count", sa.Integer(), nullable=False),
        sa.Column("frozen_patch_sha256", sa.Text(), nullable=False),
        sa.Column("frozen_submission_object_key", sa.Text(), nullable=False),
        sa.Column("changed_path_root", sa.Text(), nullable=False),
        sa.Column("final_tree_sha256", sa.Text(), nullable=False),
        sa.Column("changed_path_count", sa.Integer(), nullable=False),
        sa.Column("changed_bytes", sa.BigInteger(), nullable=False),
        sa.Column("protected_paths_intact", sa.Boolean(), nullable=False),
        sa.Column("weight_eligible", sa.Boolean(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "authoring_evidence_sha256 ~ '^[0-9a-f]{64}$' "
            "AND authoring_event_root ~ '^[0-9a-f]{64}$' "
            "AND authoring_transcript_sha256 ~ '^[0-9a-f]{64}$' "
            "AND frozen_patch_sha256 ~ '^[0-9a-f]{64}$' "
            "AND changed_path_root ~ '^[0-9a-f]{64}$' "
            "AND final_tree_sha256 ~ '^[0-9a-f]{64}$' "
            "AND signature ~ '^[0-9a-f]{128}$'",
            name="coding_shadow_authoring_freezes_integrity_check",
        ),
        sa.CheckConstraint(
            "authoring_transcript_object_key = "
            "'sha256/' || authoring_transcript_sha256 "
            "AND frozen_submission_object_key = 'sha256/' || frozen_patch_sha256",
            name="coding_shadow_authoring_freezes_object_keys_check",
        ),
        sa.CheckConstraint(
            "task_count = 1 AND changed_path_count BETWEEN 0 AND 10000 "
            "AND changed_bytes BETWEEN 0 AND 1073741824 "
            "AND authoring_transcript_bytes BETWEEN 0 AND 536870912 "
            "AND authoring_event_count BETWEEN 0 AND 1000 "
            "AND (authoring_transcript_bytes = 0) = (authoring_event_count = 0)",
            name="coding_shadow_authoring_freezes_bounds_check",
        ),
        sa.CheckConstraint(
            "weight_eligible = false",
            name="coding_shadow_authoring_freezes_weight_ineligible",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id", "run_row_id", "task_count"],
            [
                "coding_shadow_tickets.ticket_id",
                "coding_shadow_tickets.run_row_id",
                "coding_shadow_tickets.task_count",
            ],
            name="coding_shadow_authoring_freezes_ticket_fkey",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "freeze_id",
            name="coding_shadow_authoring_freezes_pkey",
        ),
        sa.UniqueConstraint(
            "ticket_id",
            name="coding_shadow_authoring_freezes_ticket_key",
        ),
    )
    op.create_index(
        "coding_shadow_authoring_freezes_run_created_idx",
        "coding_shadow_authoring_freezes",
        ["run_row_id", "created_at"],
        unique=False,
    )
    op.execute(
        """
        CREATE TRIGGER coding_shadow_authoring_freezes_append_only
        BEFORE UPDATE OR DELETE ON coding_shadow_authoring_freezes
        FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS coding_shadow_authoring_freezes_append_only "
        "ON coding_shadow_authoring_freezes"
    )
    op.drop_index(
        "coding_shadow_authoring_freezes_run_created_idx",
        table_name="coding_shadow_authoring_freezes",
    )
    op.drop_table("coding_shadow_authoring_freezes")
