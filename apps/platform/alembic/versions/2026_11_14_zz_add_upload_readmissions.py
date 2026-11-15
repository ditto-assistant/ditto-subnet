"""Label uploads that readmit the owner's own lineage (SN-485).

``upload_readmissions`` marks an accepted upload that re-runs the owner's own
unchanged agent (``identical_rescore``) or re-uploads its normalized source
under new bytes (``near_duplicate``). It is written only while the readmission
fee is active, so nothing uploaded before then is relabelled, and it changes
labels and accounting only: the upload still pays the upload fee through
``evaluation_payments``. New, cold table.

Revision ID: 6a2d9c4e8b71
Revises: 4f1c8a7d2e95
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "6a2d9c4e8b71"
down_revision: str | Sequence[str] | None = "4f1c8a7d2e95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_readmissions",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("lineage_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("miner_coldkey", sa.Text(), nullable=False),
        sa.Column("lineage_evaluation_completed", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("agent_id", name="upload_readmissions_pkey"),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            ondelete="CASCADE",
            name="upload_readmissions_agent_id_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["lineage_agent_id"],
            ["agents.agent_id"],
            ondelete="RESTRICT",
            name="upload_readmissions_lineage_agent_id_fkey",
        ),
        sa.CheckConstraint(
            "kind IN ('identical_rescore', 'near_duplicate')",
            name="upload_readmissions_kind_check",
        ),
        sa.CheckConstraint(
            "agent_id <> lineage_agent_id", name="upload_readmissions_lineage_check"
        ),
    )
    op.create_index(
        "upload_readmissions_coldkey_idx",
        "upload_readmissions",
        ["miner_coldkey", "created_at"],
    )
    op.create_index(
        "upload_readmissions_bench_version_idx",
        "upload_readmissions",
        ["bench_version", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "upload_readmissions_bench_version_idx", table_name="upload_readmissions"
    )
    op.drop_index("upload_readmissions_coldkey_idx", table_name="upload_readmissions")
    op.drop_table("upload_readmissions")
