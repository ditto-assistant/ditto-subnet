"""bind reviewer settings to screening attempts

Revision ID: e2b7c91d4a60
Revises: b5d3f8a20c71
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2b7c91d4a60"
down_revision: str | None = "b5d3f8a20c71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_attempts",
        sa.Column("review_settings_revision", sa.Integer(), nullable=True),
    )
    op.add_column(
        "screening_attempts",
        sa.Column("review_settings_instance_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "screening_attempts",
        sa.Column("review_settings_scope", sa.Text(), nullable=True),
    )
    op.add_column(
        "screening_attempts",
        sa.Column("review_settings_checksum", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "screening_attempts_review_settings_binding_check",
        "screening_attempts",
        "(review_settings_revision IS NULL AND review_settings_instance_id IS NULL "
        "AND review_settings_scope IS NULL AND review_settings_checksum IS NULL) OR "
        "(review_settings_revision > 0 AND review_settings_instance_id IS NOT NULL "
        "AND review_settings_scope IS NOT NULL "
        "AND length(review_settings_checksum) = 64)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "screening_attempts_review_settings_binding_check",
        "screening_attempts",
        type_="check",
    )
    op.drop_column("screening_attempts", "review_settings_checksum")
    op.drop_column("screening_attempts", "review_settings_scope")
    op.drop_column("screening_attempts", "review_settings_instance_id")
    op.drop_column("screening_attempts", "review_settings_revision")
