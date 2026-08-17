"""add miner sessions, device grants, oauth clients, and public profiles

Miners sign a short-lived dashboard/MCP session with the same domain-tagged
hotkey proof used for handle claims and avatars. The session is a capability
token (hashed at rest) so the public dashboard can update a profile picture
and social links without asking the miner to re-sign every click. Upload,
handle reservation, and other one-shot signed actions stay CLI-signed.

Revision ID: b7e2c9a14d80
Revises: a8c4f1d0e92b
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7e2c9a14d80"
down_revision: str | Sequence[str] | None = "a8c4f1d0e92b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "miner_profiles",
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("x_url", sa.Text(), nullable=True),
        sa.Column("github_url", sa.Text(), nullable=True),
        sa.Column("discord_handle", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("miner_hotkey", name="miner_profiles_pkey"),
        sa.CheckConstraint(
            "x_url IS NULL OR length(x_url) BETWEEN 8 AND 200",
            name="miner_profiles_x_url_len",
        ),
        sa.CheckConstraint(
            "github_url IS NULL OR length(github_url) BETWEEN 8 AND 200",
            name="miner_profiles_github_url_len",
        ),
        sa.CheckConstraint(
            "discord_handle IS NULL OR length(discord_handle) BETWEEN 2 AND 32",
            name="miner_profiles_discord_len",
        ),
        sa.CheckConstraint(
            "discord_handle IS NULL OR discord_handle ~ '^[A-Za-z0-9._]{2,32}$'",
            name="miner_profiles_discord_charset",
        ),
    )

    op.create_table(
        "miner_sessions",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("session_id", name="miner_sessions_pkey"),
        sa.CheckConstraint(
            "label IN ('dashboard', 'mcp', 'cli')",
            name="miner_sessions_label_check",
        ),
        sa.CheckConstraint(
            "length(scopes) BETWEEN 1 AND 200",
            name="miner_sessions_scopes_len",
        ),
        sa.CheckConstraint(
            "length(miner_hotkey) BETWEEN 47 AND 48",
            name="miner_sessions_hotkey_len",
        ),
    )
    op.create_index(
        "miner_sessions_hotkey_idx",
        "miner_sessions",
        ["miner_hotkey", "expires_at"],
    )

    op.create_table(
        "miner_session_tokens",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "issued_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_hash", name="miner_session_tokens_pkey"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["miner_sessions.session_id"],
            name="miner_session_tokens_session_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64",
            name="miner_session_tokens_hash_len",
        ),
    )
    op.create_index(
        "miner_session_tokens_session_idx",
        "miner_session_tokens",
        ["session_id"],
    )

    op.create_table(
        "miner_oauth_clients",
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column(
            "redirect_uris", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("client_id", name="miner_oauth_clients_pkey"),
        sa.CheckConstraint(
            "length(client_id) BETWEEN 16 AND 80",
            name="miner_oauth_clients_id_len",
        ),
        sa.CheckConstraint(
            "length(client_name) BETWEEN 1 AND 120",
            name="miner_oauth_clients_name_len",
        ),
    )

    op.create_table(
        "miner_device_grants",
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_code", sa.Text(), nullable=False),
        sa.Column("poll_token_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("oauth_client_id", sa.Text(), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=True),
        sa.Column("state", sa.Text(), nullable=True),
        sa.Column("code_challenge", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("approved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("grant_id", name="miner_device_grants_pkey"),
        sa.UniqueConstraint("user_code", name="miner_device_grants_user_code_key"),
        sa.UniqueConstraint(
            "poll_token_hash", name="miner_device_grants_poll_token_key"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["miner_sessions.session_id"],
            name="miner_device_grants_session_id_fkey",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["oauth_client_id"],
            ["miner_oauth_clients.client_id"],
            name="miner_device_grants_client_id_fkey",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'expired', 'denied', 'consumed')",
            name="miner_device_grants_status_check",
        ),
        sa.CheckConstraint(
            "user_code ~ '^[A-Z0-9]{4}-[A-Z0-9]{4}$'",
            name="miner_device_grants_user_code_fmt",
        ),
        sa.CheckConstraint(
            "ttl_seconds BETWEEN 3600 AND 2592000",
            name="miner_device_grants_ttl_range",
        ),
        sa.CheckConstraint(
            "poll_token_hash IS NULL OR length(poll_token_hash) = 64",
            name="miner_device_grants_poll_hash_len",
        ),
    )
    op.create_index(
        "miner_device_grants_status_idx",
        "miner_device_grants",
        ["status", "expires_at"],
    )

    op.create_table(
        "miner_oauth_codes",
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("grant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("code_hash", name="miner_oauth_codes_pkey"),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["miner_device_grants.grant_id"],
            name="miner_oauth_codes_grant_id_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["miner_sessions.session_id"],
            name="miner_oauth_codes_session_id_fkey",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "length(code_hash) = 64",
            name="miner_oauth_codes_hash_len",
        ),
    )

    op.create_table(
        "miner_login_nonces",
        sa.Column("nonce", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("miner_hotkey", sa.Text(), nullable=False),
        sa.Column(
            "used_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("nonce", name="miner_login_nonces_pkey"),
    )


def downgrade() -> None:
    op.drop_table("miner_login_nonces")
    op.drop_table("miner_oauth_codes")
    op.drop_index("miner_device_grants_status_idx", table_name="miner_device_grants")
    op.drop_table("miner_device_grants")
    op.drop_table("miner_oauth_clients")
    op.drop_index("miner_session_tokens_session_idx", table_name="miner_session_tokens")
    op.drop_table("miner_session_tokens")
    op.drop_index("miner_sessions_hotkey_idx", table_name="miner_sessions")
    op.drop_table("miner_sessions")
    op.drop_table("miner_profiles")
