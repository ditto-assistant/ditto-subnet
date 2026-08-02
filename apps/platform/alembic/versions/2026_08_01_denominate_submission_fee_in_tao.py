"""Denominate the operator-controlled miner submission fee directly in TAO."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d42a17ef03"
down_revision: str | Sequence[str] | None = "a7c41f8b2e93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_FEE_RAO = 40_000_000


def upgrade() -> None:
    op.add_column(
        "submission_settings_revisions",
        sa.Column(
            "fee_amount_rao",
            sa.BigInteger(),
            nullable=False,
            server_default=str(_DEFAULT_FEE_RAO),
        ),
    )
    op.create_check_constraint(
        "submission_settings_fee_amount_rao_check",
        "submission_settings_revisions",
        "fee_amount_rao BETWEEN 1 AND 1000000000000",
    )
    op.add_column(
        "upload_admission_reservations",
        sa.Column(
            "fee_amount_rao",
            sa.BigInteger(),
            nullable=False,
            server_default=str(_DEFAULT_FEE_RAO),
        ),
    )
    op.create_check_constraint(
        "upload_admission_fee_amount_rao_check",
        "upload_admission_reservations",
        "fee_amount_rao BETWEEN 1 AND 1000000000000",
    )
    op.alter_column(
        "submission_settings_revisions", "fee_amount_rao", server_default=None
    )
    op.alter_column(
        "upload_admission_reservations", "fee_amount_rao", server_default=None
    )


def downgrade() -> None:
    op.drop_constraint(
        "upload_admission_fee_amount_rao_check",
        "upload_admission_reservations",
        type_="check",
    )
    op.drop_column("upload_admission_reservations", "fee_amount_rao")
    op.drop_constraint(
        "submission_settings_fee_amount_rao_check",
        "submission_settings_revisions",
        type_="check",
    )
    op.drop_column("submission_settings_revisions", "fee_amount_rao")
