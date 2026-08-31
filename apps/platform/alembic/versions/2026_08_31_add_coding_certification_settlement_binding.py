"""persist terminal canary settlement bindings on coding certifications

Revision ID: f7d2c9a4e681
Revises: b8c1e4a7d359
Create Date: 2026-08-31

Legacy certification rows intentionally remain unbound. They stay auditable but
cannot authorize private coding assignment, ticket issuance, or task claims.
New invoked-model receipts write these fields only after Platform has locked and
verified the terminal claimed-lease settlement ledger.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7d2c9a4e681"
down_revision: str | Sequence[str] | None = "b8c1e4a7d359"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "coding_capability_certifications",
        sa.Column("settlement_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "coding_capability_certifications",
        sa.Column("settlement_inference_grant_sha256", sa.Text(), nullable=True),
    )
    op.add_column(
        "coding_capability_certifications",
        sa.Column("settlement_provider_receipt_set_sha256", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "coding_certifications_settlement_binding_check",
        "coding_capability_certifications",
        "((settlement_generation IS NULL "
        "AND settlement_inference_grant_sha256 IS NULL "
        "AND settlement_provider_receipt_set_sha256 IS NULL) "
        "OR (settlement_generation BETWEEN 1 AND 2147483647 "
        "AND settlement_inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
        "AND settlement_provider_receipt_set_sha256 ~ '^[0-9a-f]{64}$'))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "coding_certifications_settlement_binding_check",
        "coding_capability_certifications",
        type_="check",
    )
    op.drop_column(
        "coding_capability_certifications",
        "settlement_provider_receipt_set_sha256",
    )
    op.drop_column(
        "coding_capability_certifications",
        "settlement_inference_grant_sha256",
    )
    op.drop_column("coding_capability_certifications", "settlement_generation")
