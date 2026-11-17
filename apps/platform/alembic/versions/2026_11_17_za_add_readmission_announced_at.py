"""Add the readmission announcement date to miner submission settings (SN-485).

``readmission_announced_at`` is when Ditto announced the readmission fee. It is
null on every existing revision and stays null until an operator writes it, and
while it is null nothing is charged as a readmission and self-serve
readmission is unavailable. The fee goes live only once the active benchmark
is v19 or later and seven days have passed since this date; see
:mod:`ditto.db.queries.readmission_gate`.

Revision ID: 9d4b7e2a6c18
Revises: d307b3894c95
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9d4b7e2a6c18"
down_revision: str | Sequence[str] | None = "d307b3894c95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A nullable column with no default is a metadata-only add, and this
    # append-only operator table is tiny and cold.
    op.add_column(
        "submission_settings_revisions",
        sa.Column(
            "readmission_announced_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("submission_settings_revisions", "readmission_announced_at")
