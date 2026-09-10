"""Copy-hold triage court: settings revisions and shadow recommendations.

Revision ID: 1df3bb6d684a
Revises: b3f926ce145a
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "1df3bb6d684a"
down_revision = "653d95402c0f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "copy_court_settings_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "length(checksum) = 64", name="copy_court_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="copy_court_settings_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="copy_court_settings_actor_check",
        ),
        sa.UniqueConstraint("parent_revision", name="copy_court_settings_parent_key"),
    )
    op.create_table(
        "ath_copy_court_recommendations",
        sa.Column("recommendation_id", sa.UUID(), primary_key=True),
        sa.Column("review_id", sa.UUID(), nullable=False),
        sa.Column("agent_id", sa.UUID(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("hold_class", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("settings_checksum", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_revision", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["review_id"], ["ath_reviews.review_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["settings_revision"],
            ["copy_court_settings_revisions.revision"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "verdict IN ('clear', 'reject', 'escalate')",
            name="ath_copy_court_recommendations_verdict_check",
        ),
        sa.CheckConstraint(
            "hold_class IN ('near_duplicate', "
            "'rejected_resubmission_byte_identical', "
            "'rejected_resubmission_repack', "
            "'rejected_resubmission_cross_miner', 'unknown')",
            name="ath_copy_court_recommendations_hold_class_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 3",
            name="ath_copy_court_recommendations_reason_check",
        ),
        sa.CheckConstraint(
            "length(settings_checksum) = 64",
            name="ath_copy_court_recommendations_checksum_check",
        ),
        sa.CheckConstraint(
            "(model IS NULL AND prompt_revision IS NULL) OR "
            "(model IS NOT NULL AND prompt_revision IS NOT NULL)",
            name="ath_copy_court_recommendations_model_pair_check",
        ),
        sa.UniqueConstraint(
            "review_id",
            "settings_revision",
            name="ath_copy_court_recommendations_review_revision_key",
        ),
    )
    op.create_index(
        "ath_copy_court_recommendations_agent_idx",
        "ath_copy_court_recommendations",
        ["agent_id", "created_at"],
    )
    op.create_index(
        "ath_copy_court_recommendations_created_idx",
        "ath_copy_court_recommendations",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ath_copy_court_recommendations_created_idx",
        table_name="ath_copy_court_recommendations",
    )
    op.drop_index(
        "ath_copy_court_recommendations_agent_idx",
        table_name="ath_copy_court_recommendations",
    )
    op.drop_table("ath_copy_court_recommendations")
    op.drop_table("copy_court_settings_revisions")
