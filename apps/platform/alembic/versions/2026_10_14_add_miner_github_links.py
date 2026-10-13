"""Add miner_github_links append-only table with partial unique indexes on active links.

Revision ID: d952a81c42e3
Revises: a4963e2b18c7
Create Date: 2026-10-14
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d952a81c42e3"
down_revision: str | None = "a4963e2b18c7"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "miner_github_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("hotkey", sa.String(length=64), nullable=False),
        sa.Column("coldkey", sa.String(length=64), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.String(length=64), nullable=False),
        sa.Column("key_kind", sa.String(length=16), nullable=False),
        sa.Column("signer", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=128), nullable=False),
        sa.Column("nonce", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("key_kind IN ('hotkey', 'coldkey')", name="miner_github_links_key_kind_check"),
    )

    # Partial unique indexes: at most one active GitHub link per hotkey,
    # and at most one active hotkey link per numeric github_user_id.
    op.create_index(
        "uq_miner_github_links_active_hotkey",
        "miner_github_links",
        ["hotkey"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_index(
        "uq_miner_github_links_active_github_user_id",
        "miner_github_links",
        ["github_user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_miner_github_links_active_github_user_id", table_name="miner_github_links")
    op.drop_index("uq_miner_github_links_active_hotkey", table_name="miner_github_links")
    op.drop_table("miner_github_links")
