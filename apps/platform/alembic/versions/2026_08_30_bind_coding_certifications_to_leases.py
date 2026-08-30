"""bind coding capability receipts to claimed certification leases

Revision ID: c1f8d4a0b367
Revises: b3e7c1a0d492
Create Date: 2026-08-30

New receipts must name a claimed public-canary lease. Historical rows keep a
nullable lease_id. Coding contract v1 stays weight_eligible=false.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1f8d4a0b367"
down_revision: str | Sequence[str] | None = "b3e7c1a0d492"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "coding_capability_certifications",
        sa.Column("lease_id", sa.UUID(), nullable=True),
    )
    op.create_foreign_key(
        "coding_certifications_lease_fkey",
        "coding_capability_certifications",
        "coding_certification_leases",
        ["lease_id"],
        ["lease_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "coding_certifications_lease_key",
        "coding_capability_certifications",
        ["lease_id"],
        unique=True,
        postgresql_where=sa.text("lease_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "coding_certifications_lease_key",
        table_name="coding_capability_certifications",
    )
    op.drop_constraint(
        "coding_certifications_lease_fkey",
        "coding_capability_certifications",
        type_="foreignkey",
    )
    op.drop_column("coding_capability_certifications", "lease_id")
