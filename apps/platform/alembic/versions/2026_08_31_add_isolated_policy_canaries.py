"""Keep policy canaries isolated from ordinary screening traffic.

Revision ID: 7f2d9ab4c6e1
Revises: 6ec0a1d5b814
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7f2d9ab4c6e1"
down_revision: str | Sequence[str] | None = "6ec0a1d5b814"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screener_policy_activations",
        sa.Column(
            "canary_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "scored_policy_rescreen_releases",
        sa.Column("review_settings_revision", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "scored_policy_rescreen_releases_review_settings_fkey",
        "scored_policy_rescreen_releases",
        "screener_review_settings_revisions",
        ["review_settings_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "scored_policy_rescreen_releases_review_settings_fkey",
        "scored_policy_rescreen_releases",
        type_="foreignkey",
    )
    op.drop_column("scored_policy_rescreen_releases", "review_settings_revision")
    op.drop_column("screener_policy_activations", "canary_only")
