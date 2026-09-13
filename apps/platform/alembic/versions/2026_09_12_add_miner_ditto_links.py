"""Miner ↔ Ditto account links (Sign in with Ditto) and their link attempts.

Revision ID: 7a2c91d4e5f0
Revises: 1df3bb6d684a
"""

import sqlalchemy as sa

from alembic import op

revision = "7a2c91d4e5f0"
down_revision = "1df3bb6d684a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # One active Ditto account per hotkey. The Ditto user id comes only from a
    # verified OIDC id_token; the hotkey only from a live hotkey-signed miner
    # session. Many hotkeys may point at the same Ditto account.
    op.create_table(
        "miner_ditto_links",
        sa.Column("miner_hotkey", sa.Text(), primary_key=True),
        sa.Column("ditto_user_id", sa.Text(), nullable=False),
        sa.Column("ditto_email", sa.Text(), nullable=True),
        sa.Column("miner_coldkey", sa.Text(), nullable=True),
        sa.Column("linked_via", sa.Text(), nullable=False),
        sa.Column("session_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(ditto_user_id) BETWEEN 1 AND 128",
            name="miner_ditto_links_user_id_len",
        ),
        sa.CheckConstraint(
            "linked_via IN ('dashboard', 'cli')",
            name="miner_ditto_links_linked_via_check",
        ),
    )
    op.create_index(
        "miner_ditto_links_user_idx", "miner_ditto_links", ["ditto_user_id"]
    )

    # One authorization-code round trip. The state is stored hashed and is the
    # only thing the public callback can present; the attempt row is what
    # binds that callback to the hotkey whose session started it.
    op.create_table(
        "miner_ditto_link_attempts",
        sa.Column("attempt_id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("session_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("code_verifier", sa.Text(), nullable=False),
        sa.Column("client", sa.Text(), nullable=False),
        sa.Column("return_to", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("ditto_user_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["miner_sessions.session_id"],
            ondelete="CASCADE",
            name="miner_ditto_link_attempts_session_id_fkey",
        ),
        sa.CheckConstraint(
            "length(state_hash) = 64", name="miner_ditto_link_attempts_state_len"
        ),
        sa.CheckConstraint(
            "client IN ('dashboard', 'cli')",
            name="miner_ditto_link_attempts_client_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'linked', 'failed', 'expired')",
            name="miner_ditto_link_attempts_status_check",
        ),
        sa.UniqueConstraint("state_hash", name="miner_ditto_link_attempts_state_key"),
    )
    op.create_index(
        "miner_ditto_link_attempts_hotkey_idx",
        "miner_ditto_link_attempts",
        ["miner_hotkey", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "miner_ditto_link_attempts_hotkey_idx", table_name="miner_ditto_link_attempts"
    )
    op.drop_table("miner_ditto_link_attempts")
    op.drop_index("miner_ditto_links_user_idx", table_name="miner_ditto_links")
    op.drop_table("miner_ditto_links")
