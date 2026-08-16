"""add miner_avatars: signed hotkey profile pictures

Revision ID: c3f8a1b04e21
Revises: a8c1e4f29b70
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f8a1b04e21"
down_revision: str | Sequence[str] | None = "a8c1e4f29b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "miner_avatars",
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("miner_hotkey", name="miner_avatars_pkey"),
        sa.UniqueConstraint("nonce", name="miner_avatars_nonce_key"),
        sa.CheckConstraint(
            "length(sha256) = 64", name="miner_avatars_sha256_check"
        ),
        sa.CheckConstraint(
            "content_type IN ('image/png', 'image/jpeg', 'image/webp')",
            name="miner_avatars_content_type_check",
        ),
    )
    op.create_table(
        "miner_avatar_nonces",
        sa.Column("nonce", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column(
            "used_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("nonce", name="miner_avatar_nonces_pkey"),
    )
    op.create_index(
        "miner_avatar_nonces_hotkey_idx",
        "miner_avatar_nonces",
        ["miner_hotkey"],
    )


def downgrade() -> None:
    op.drop_index("miner_avatar_nonces_hotkey_idx", table_name="miner_avatar_nonces")
    op.drop_table("miner_avatar_nonces")
    op.drop_table("miner_avatars")
