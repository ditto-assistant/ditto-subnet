"""merge screening-submission search and transcript mirror settings heads

#2256 (``7bc0400d90c2``) and #2204 (``b7e2c9a41d08``) both chained onto
``c3a91e7b2d40`` and merged minutes apart. They touch unrelated tables
(``agents``/``evaluation_payments`` indexes vs the new
``transcript_mirror_settings_revisions`` table), so the merge is a no-op.

Revision ID: 5d1f7a93c2e8
Revises: 7bc0400d90c2, b7e2c9a41d08
Create Date: 2026-09-26
"""

from collections.abc import Sequence

revision: str = "5d1f7a93c2e8"
down_revision: str | Sequence[str] | None = (
    "7bc0400d90c2",
    "b7e2c9a41d08",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
