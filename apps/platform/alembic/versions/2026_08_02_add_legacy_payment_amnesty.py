"""Add a bounded, auditable amnesty for pre-cutover upload payments."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4f6a8b1c2e3"
down_revision: str | Sequence[str] | None = "c9d42a17ef03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "upload_admission_reservations",
        sa.Column(
            "legacy_payment_cutoff_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.execute(
        """
        UPDATE upload_admission_reservations
        SET legacy_payment_cutoff_at = CURRENT_TIMESTAMP
        WHERE expires_at > CURRENT_TIMESTAMP
        """
    )
    op.add_column(
        "evaluation_payments",
        sa.Column(
            "accepted_under_legacy_fee_amnesty",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column(
        "evaluation_payments",
        "accepted_under_legacy_fee_amnesty",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("evaluation_payments", "accepted_under_legacy_fee_amnesty")
    op.drop_column("upload_admission_reservations", "legacy_payment_cutoff_at")
