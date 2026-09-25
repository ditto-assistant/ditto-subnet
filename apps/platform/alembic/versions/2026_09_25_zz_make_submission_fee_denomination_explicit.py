"""Make the miner submission fee denomination explicit on every revision.

Revision ID: 47cca3c5d880
Revises: 9e4c7a1b6d20

Every existing revision already prices uploads in exact rao: the quote and the
verified payment are ``fee_amount_rao``, and TAO/USD is reporting metadata
only. Backfilling ``fixed_tao`` therefore records the behaviour in force rather
than changing it; no fee, cooldown, revision number, or timestamp is touched.
The server default is dropped after the backfill so a future writer must name
the denomination, and the CHECK admits only the reviewed ``fixed_tao`` mode.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "47cca3c5d880"
down_revision: str | Sequence[str] | None = "9e4c7a1b6d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "submission_settings_revisions",
        sa.Column(
            "fee_denomination",
            sa.Text(),
            nullable=False,
            server_default="fixed_tao",
        ),
    )
    op.create_check_constraint(
        "submission_settings_fee_denomination_check",
        "submission_settings_revisions",
        "fee_denomination = 'fixed_tao'",
    )
    op.alter_column(
        "submission_settings_revisions", "fee_denomination", server_default=None
    )


def downgrade() -> None:
    op.drop_constraint(
        "submission_settings_fee_denomination_check",
        "submission_settings_revisions",
        type_="check",
    )
    op.drop_column("submission_settings_revisions", "fee_denomination")
