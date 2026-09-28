"""Append immutable automated and manual source-review events.

Revision ID: 9d8c2e4a7f10
Revises: 1bc9d8a6207e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "9d8c2e4a7f10"
down_revision: str | Sequence[str] | None = "1bc9d8a6207e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_review_events",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quarantine_id", postgresql.UUID(as_uuid=True)),
        sa.Column("resolution_id", postgresql.UUID(as_uuid=True)),
        sa.Column("previous_event_id", postgresql.UUID(as_uuid=True)),
        sa.Column("event_kind", sa.Text(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reviewer_model", sa.Text()),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("effective_decision", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("prior_agent_status", sa.Text(), nullable=False),
        sa.Column("next_agent_status", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["previous_event_id"],
            ["screening_review_events.event_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "event_kind IN ('automated', 'manual')", name="sre_kind_check"
        ),
        sa.CheckConstraint("length(artifact_sha256) = 64", name="sre_sha_check"),
        sa.CheckConstraint("policy_version > 0", name="sre_policy_check"),
        sa.CheckConstraint(
            "effective_decision IN ('reject', 'hold', 'pass', "
            "'provisional_admission', 'no_change', 'release', 'rescreen')",
            name="sre_decision_check",
        ),
        sa.CheckConstraint("length(trim(actor)) > 0", name="sre_actor_check"),
        sa.UniqueConstraint("resolution_id", name="sre_resolution_key"),
    )
    op.create_index(
        "sre_automated_attempt_key",
        "screening_review_events",
        ["attempt_id"],
        unique=True,
        postgresql_where=sa.text("event_kind = 'automated'"),
    )
    op.create_index(
        "sre_agent_created_idx",
        "screening_review_events",
        ["agent_id", "created_at", "event_id"],
    )
    op.create_index(
        "sre_created_idx", "screening_review_events", ["created_at", "event_id"]
    )
    op.execute(
        "CREATE FUNCTION reject_screening_review_event_mutation() RETURNS trigger "
        "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION "
        "'screening_review_events is append-only'; END $$"
    )
    op.execute(
        "CREATE TRIGGER screening_review_events_immutable BEFORE UPDATE OR DELETE "
        "ON screening_review_events FOR EACH ROW "
        "EXECUTE FUNCTION reject_screening_review_event_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER screening_review_events_immutable ON screening_review_events"
    )
    op.execute("DROP FUNCTION reject_screening_review_event_mutation()")
    op.drop_index("sre_agent_created_idx", table_name="screening_review_events")
    op.drop_index("sre_created_idx", table_name="screening_review_events")
    op.drop_index("sre_automated_attempt_key", table_name="screening_review_events")
    op.drop_table("screening_review_events")
