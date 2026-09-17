"""Feedback Track contributions: identity plumbing, no reward economics.

Revision ID: 3c8f0a1d9b27
Revises: 7a2c91d4e5f0
"""

import sqlalchemy as sa

from alembic import op

revision = "3c8f0a1d9b27"
down_revision = "7a2c91d4e5f0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One row per (source, external_ref, kind): a Ditto feedback report or a
    # follow-up on it, credited to a Ditto account. Hotkeys are resolved at
    # read time through miner_ditto_links, so a later link still counts and a
    # revoked one stops counting. `weight` is stored for a future policy and
    # is consumed by nothing here.
    op.create_table(
        "feedback_track_contributions",
        sa.Column("contribution_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("ditto_user_id", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_ref", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("weight", sa.Numeric(12, 6), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(ditto_user_id) BETWEEN 1 AND 128",
            name="feedback_track_user_id_len",
        ),
        sa.CheckConstraint(
            "source IN ('ditto_feedback')", name="feedback_track_source_check"
        ),
        sa.CheckConstraint(
            "kind IN ('report', 'follow_up', 'shipped')",
            name="feedback_track_kind_check",
        ),
        sa.CheckConstraint(
            "length(external_ref) BETWEEN 1 AND 200",
            name="feedback_track_external_ref_len",
        ),
        sa.CheckConstraint(
            "weight IS NULL OR weight >= 0", name="feedback_track_weight_nonneg"
        ),
        sa.UniqueConstraint(
            "source", "external_ref", "kind", name="feedback_track_dedupe_key"
        ),
    )
    op.create_index(
        "feedback_track_user_idx",
        "feedback_track_contributions",
        ["ditto_user_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("feedback_track_user_idx", table_name="feedback_track_contributions")
    op.drop_table("feedback_track_contributions")
