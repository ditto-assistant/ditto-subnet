"""Store future v13 window activation and explicit artifact deadline bindings.

Revision ID: 8a4d27c0f639
Revises: 7f5e3a91bc42
Create Date: 2026-09-23

Storage only. No activation or per-artifact window is inserted by this
migration, and no terminal decision is enabled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8a4d27c0f639"
down_revision: str | Sequence[str] | None = "7f5e3a91bc42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_review_deadline_activations",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("policy_digest", sa.Text(), nullable=False),
        sa.Column("activate_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("policy_version >= 13", name="srda_policy_check"),
        sa.CheckConstraint("length(policy_digest) = 64", name="srda_digest_check"),
        sa.CheckConstraint(
            "window_seconds BETWEEN 3600 AND 604800", name="srda_window_check"
        ),
        sa.CheckConstraint("activate_at >= created_at", name="srda_no_backdate_check"),
        sa.CheckConstraint("length(trim(reason)) >= 8", name="srda_reason_check"),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120", name="srda_actor_check"
        ),
    )
    op.create_index(
        "srda_policy_activate_idx",
        "screening_review_deadline_activations",
        ["policy_version", "activate_at", "revision"],
    )
    op.execute(
        """
        CREATE FUNCTION stamp_review_deadline_activation_created_at()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.created_at := clock_timestamp();
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER review_deadline_activation_server_time
        BEFORE INSERT ON screening_review_deadline_activations
        FOR EACH ROW EXECUTE FUNCTION stamp_review_deadline_activation_created_at()
        """
    )
    op.execute(
        """
        CREATE FUNCTION reject_review_deadline_activation_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'review deadline activations are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER review_deadline_activation_immutable
        BEFORE UPDATE OR DELETE ON screening_review_deadline_activations
        FOR EACH ROW EXECUTE FUNCTION reject_review_deadline_activation_mutation()
        """
    )
    op.create_table(
        "screening_review_windows",
        sa.Column("window_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("activation_revision", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("manifest_digest", sa.Text(), nullable=False),
        sa.Column("start_event", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["first_attempt_id"], ["screening_attempts.attempt_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["activation_revision"],
            ["screening_review_deadline_activations.revision"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("agent_id", "policy_version", name="srw_agent_policy_key"),
        sa.CheckConstraint("length(artifact_sha256) = 64", name="srw_artifact_check"),
        sa.CheckConstraint("length(manifest_digest) = 64", name="srw_manifest_check"),
        sa.CheckConstraint("policy_version >= 13", name="srw_policy_check"),
        sa.CheckConstraint("length(trim(start_event)) >= 3", name="srw_event_check"),
        sa.CheckConstraint("deadline_at > started_at", name="srw_deadline_check"),
    )
    op.execute(
        """
        CREATE FUNCTION reject_review_window_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'review windows are append-only';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER review_window_immutable
        BEFORE UPDATE OR DELETE ON screening_review_windows
        FOR EACH ROW EXECUTE FUNCTION reject_review_window_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER review_window_immutable ON screening_review_windows")
    op.execute("DROP FUNCTION reject_review_window_mutation()")
    op.drop_table("screening_review_windows")
    op.execute(
        "DROP TRIGGER review_deadline_activation_server_time "
        "ON screening_review_deadline_activations"
    )
    op.execute("DROP FUNCTION stamp_review_deadline_activation_created_at()")
    op.execute(
        "DROP TRIGGER review_deadline_activation_immutable "
        "ON screening_review_deadline_activations"
    )
    op.execute("DROP FUNCTION reject_review_deadline_activation_mutation()")
    op.drop_index(
        "srda_policy_activate_idx",
        table_name="screening_review_deadline_activations",
    )
    op.drop_table("screening_review_deadline_activations")
