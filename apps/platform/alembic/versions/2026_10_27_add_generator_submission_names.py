"""Let miners name their generator submissions.

``generator_submissions.name`` is the optional miner-chosen display name the
public generator board, round detail and submission drawer show, the way
``agents.name`` names an agent. Unnamed submissions stay ``NULL`` and render
a short submission id. The cap matches agent names (64 characters).

``generator_submissions`` is a small, cold table (one row per generator
upload), so a nullable add with no default is metadata-only and the check
constraint's validation scan is brief.

Revision ID: 3d8f1a6c2e57
Revises: 5b7e1c9d3a42
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3d8f1a6c2e57"
down_revision: str | Sequence[str] | None = "5b7e1c9d3a42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("generator_submissions", sa.Column("name", sa.Text(), nullable=True))
    op.create_check_constraint(
        "generator_submissions_name_check",
        "generator_submissions",
        "name IS NULL OR char_length(name) BETWEEN 1 AND 64",
    )


def downgrade() -> None:
    op.drop_constraint(
        "generator_submissions_name_check", "generator_submissions", type_="check"
    )
    op.drop_column("generator_submissions", "name")
