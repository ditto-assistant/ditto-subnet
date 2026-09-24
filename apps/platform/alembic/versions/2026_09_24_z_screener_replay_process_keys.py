"""Operator-pinned replay process keys and durable one-use nonces.

Revision ID: b87d2e4f10a9
Revises: d7b4f150ae2c
Create Date: 2026-09-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b87d2e4f10a9"
down_revision: str | Sequence[str] | None = "d7b4f150ae2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screener_replay_process_keys",
        sa.Column("key_sha256", sa.Text(), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("instance_id", sa.Text(), nullable=False),
        sa.Column("public_key_hex", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True)),
        sa.ForeignKeyConstraint(["node_id"], ["screener_nodes.node_id"]),
        sa.CheckConstraint(
            "length(key_sha256) = 64", name="srpk_key_sha256_length_check"
        ),
        sa.CheckConstraint(
            "length(public_key_hex) = 64", name="srpk_public_key_hex_length_check"
        ),
        sa.CheckConstraint("revision > 0", name="srpk_revision_check"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="srpk_status_check"),
    )
    op.create_index(
        "srpk_one_active_instance_idx",
        "screener_replay_process_keys",
        ["node_id", "instance_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.add_column(
        "screening_verification_replays",
        sa.Column("process_key_sha256", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "svrp_process_key_fk",
        "screening_verification_replays",
        "screener_replay_process_keys",
        ["process_key_sha256"],
        ["key_sha256"],
    )
    op.create_table(
        "screener_replay_process_nonces",
        sa.Column("key_sha256", sa.Text(), primary_key=True),
        sa.Column("nonce", sa.Text(), primary_key=True),
        sa.Column("consumed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["key_sha256"], ["screener_replay_process_keys.key_sha256"]
        ),
        sa.CheckConstraint("length(nonce) = 32", name="srpn_nonce_length_check"),
    )
    op.create_index(
        "srpn_consumed_at_idx", "screener_replay_process_nonces", ["consumed_at"]
    )


def downgrade() -> None:
    op.drop_index("srpn_consumed_at_idx", table_name="screener_replay_process_nonces")
    op.drop_table("screener_replay_process_nonces")
    op.drop_constraint(
        "svrp_process_key_fk", "screening_verification_replays", type_="foreignkey"
    )
    op.drop_column("screening_verification_replays", "process_key_sha256")
    op.drop_index(
        "srpk_one_active_instance_idx", table_name="screener_replay_process_keys"
    )
    op.drop_table("screener_replay_process_keys")
