"""Add an audited live submission deposit address.

Revision ID: e8a4c91d7f20
Revises: b7c4e1a90d52
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8a4c91d7f20"
down_revision: str | Sequence[str] | None = "f2a7c9e41b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submission_deposit_address_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("payment_address", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "payment_address ~ '^[1-9A-HJ-NP-Za-km-z]{32,64}$'",
            name="submission_deposit_address_ss58_check",
        ),
        sa.CheckConstraint(
            "parent_revision >= 0",
            name="submission_deposit_address_parent_revision_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="submission_deposit_address_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="submission_deposit_address_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision",
            name="submission_deposit_address_parent_revision_key",
        ),
    )
    # Existing reservations predate the live control and were quoted against
    # boot config, which Alembic intentionally cannot read. The first address
    # change atomically backfills these nullable rows with the old effective
    # address before activating the new one.
    op.add_column(
        "upload_admission_reservations",
        sa.Column("payment_send_address", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("upload_admission_reservations", "payment_send_address")
    op.drop_table("submission_deposit_address_revisions")
