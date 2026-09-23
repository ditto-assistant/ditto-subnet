"""Separate normative policy text from the worker module manifest.

Revision ID: 9e2c4f7a6b10
Revises: 5c7e219a0d4b
Create Date: 2026-09-23

Existing activation rows remain legacy/unverified until an explicitly
scheduled revision binds both identities. No activation or window is created.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9e2c4f7a6b10"
down_revision: str | Sequence[str] | None = "5c7e219a0d4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "screening_review_deadline_activations",
        sa.Column("policy_document_digest", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "srda_document_digest_check",
        "screening_review_deadline_activations",
        "policy_document_digest IS NULL OR length(policy_document_digest) = 64",
    )


def downgrade() -> None:
    op.drop_constraint(
        "srda_document_digest_check", "screening_review_deadline_activations"
    )
    op.drop_column("screening_review_deadline_activations", "policy_document_digest")
