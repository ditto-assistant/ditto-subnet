"""Add per-competition fee overrides to miner submission settings.

``competition_fees`` maps a competition id (``generator``, and later
``coding``/``router``) to ``{"fee_amount_rao": int | null, "fee_required":
bool | null}``; ``null`` inherits. Every existing revision reads as ``{}``,
which inherits everything, so the agent and generator lanes charge exactly
what they charged before until an operator writes an override.

Revision ID: 5b7e1c9d3a42
Revises: 9e4b2d7c1a63
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "5b7e1c9d3a42"
down_revision: str | Sequence[str] | None = "9e4b2d7c1a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A constant server default is a metadata-only add on Postgres 11+, and
    # this append-only operator table is tiny and cold.
    op.add_column(
        "submission_settings_revisions",
        sa.Column(
            "competition_fees",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "submission_settings_competition_fees_check",
        "submission_settings_revisions",
        "jsonb_typeof(competition_fees) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "submission_settings_competition_fees_check",
        "submission_settings_revisions",
        type_="check",
    )
    op.drop_column("submission_settings_revisions", "competition_fees")
