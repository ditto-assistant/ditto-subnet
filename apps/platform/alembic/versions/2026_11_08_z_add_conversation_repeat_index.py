"""Repeat assessments: fresh-seed samples of one artifact to measure judge noise.

``repeat_index`` 0 is the canonical assessment an artifact is scored by; 1..N
are shadow-only repeats with their own seed. The identity index now includes
it, so one canonical and one row per repeat index can coexist per artifact.
"""

import sqlalchemy as sa

from alembic import op

revision = "c4d8e2f1a937"
down_revision = "b7e3c1d9a420"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_assessments",
        sa.Column(
            "repeat_index", sa.SmallInteger(), nullable=False, server_default="0"
        ),
    )
    op.create_check_constraint(
        "conversation_repeat_index_range",
        "conversation_assessments",
        "repeat_index >= 0 AND repeat_index <= 8",
    )
    op.drop_index(
        "conversation_assessment_identity", table_name="conversation_assessments"
    )
    op.create_index(
        "conversation_assessment_identity",
        "conversation_assessments",
        ["agent_id", "artifact_sha256", "bench_version", "instrument", "repeat_index"],
        unique=True,
        postgresql_where=sa.text("retry_of IS NULL"),
    )


def downgrade() -> None:
    op.execute("DELETE FROM conversation_assessments WHERE repeat_index > 0")
    op.drop_index(
        "conversation_assessment_identity", table_name="conversation_assessments"
    )
    op.create_index(
        "conversation_assessment_identity",
        "conversation_assessments",
        ["agent_id", "artifact_sha256", "bench_version", "instrument"],
        unique=True,
        postgresql_where=sa.text("retry_of IS NULL"),
    )
    op.drop_constraint(
        "conversation_repeat_index_range", "conversation_assessments", type_="check"
    )
    op.drop_column("conversation_assessments", "repeat_index")
