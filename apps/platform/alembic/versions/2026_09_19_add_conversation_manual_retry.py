"""One explicitly authorized retry, preserving the original attempt ledger."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b2af680e139d"
down_revision = "ae67182d095c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_assessments", sa.Column("retry_of", sa.Uuid()))
    op.add_column(
        "conversation_assessments",
        sa.Column("retry_authorization", postgresql.JSONB(none_as_null=True)),
    )
    op.create_foreign_key(
        "conversation_retry_parent",
        "conversation_assessments",
        "conversation_assessments",
        ["retry_of"],
        ["assessment_id"],
    )
    op.create_unique_constraint(
        "conversation_one_manual_retry", "conversation_assessments", ["retry_of"]
    )
    op.drop_constraint(
        "conversation_assessment_identity", "conversation_assessments", type_="unique"
    )
    op.create_index(
        "conversation_assessment_identity",
        "conversation_assessments",
        ["agent_id", "artifact_sha256", "bench_version", "instrument"],
        unique=True,
        postgresql_where=sa.text("retry_of IS NULL"),
    )
    op.create_check_constraint(
        "conversation_retry_lineage",
        "conversation_assessments",
        "retry_of IS NULL OR "
        "(retry_of <> assessment_id AND retry_authorization IS NULL)",
    )


def downgrade() -> None:
    # Refuse to discard paid retry history or collide with the old unique key.
    if op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM conversation_assessments "
            "WHERE retry_of IS NOT NULL OR retry_authorization IS NOT NULL"
        )
    ):
        raise RuntimeError("cannot downgrade audited conversation retry history")
    op.drop_constraint(
        "conversation_retry_lineage", "conversation_assessments", type_="check"
    )
    op.drop_index(
        "conversation_assessment_identity", table_name="conversation_assessments"
    )
    op.create_unique_constraint(
        "conversation_assessment_identity",
        "conversation_assessments",
        ["agent_id", "artifact_sha256", "bench_version", "instrument"],
    )
    op.drop_constraint(
        "conversation_one_manual_retry", "conversation_assessments", type_="unique"
    )
    op.drop_constraint(
        "conversation_retry_parent", "conversation_assessments", type_="foreignkey"
    )
    op.drop_column("conversation_assessments", "retry_authorization")
    op.drop_column("conversation_assessments", "retry_of")
