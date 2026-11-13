"""Ditto Edge shadow lane (SN-481): settings, nodes, objects, challenges, ledger.

Opt-in Mac nodes hold public, content-addressed objects that Platform assigns
and challenges. Everything here is new and touches no existing table; the
lane is off until an operator writes a ``shadow`` settings revision.

``edge_ledger_windows.weight_eligible`` is pinned false by a check constraint:
these rows show what a node would earn and pay nothing. Paying from this lane
takes a migration that drops that constraint.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b44f233dc32f"
down_revision = "c3e8a2f61d4b"
branch_labels = None
depends_on = None

_TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "edge_lane_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("settings", postgresql.JSONB(), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("scope = '*'", name="edge_lane_settings_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="edge_lane_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0", name="edge_lane_settings_parent_revision_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="edge_lane_settings_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="edge_lane_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope", "parent_revision", name="edge_lane_settings_scope_parent_key"
        ),
    )

    op.create_table(
        "edge_lane_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("placement_salt", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="edge_lane_state_singleton_check"),
        sa.CheckConstraint(
            "length(placement_salt) = 32", name="edge_lane_state_salt_length_check"
        ),
    )

    op.create_table(
        "edge_nodes",
        sa.Column("node_id", sa.Text(), primary_key=True),
        sa.Column("public_key_hex", sa.Text(), nullable=False, unique=True),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("hotkey_ss58", sa.Text(), nullable=True),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("client_version", sa.Text(), nullable=False),
        sa.Column("platform", postgresql.JSONB(), nullable=False),
        sa.Column("quota_bytes", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("strikes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("enrolled_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("last_poll_at", _TS, nullable=True),
        sa.CheckConstraint(
            "node_id ~ '^edn_[0-9a-f]{32}$'", name="edge_nodes_id_check"
        ),
        sa.CheckConstraint(
            "public_key_hex ~ '^[0-9a-f]{64}$'", name="edge_nodes_public_key_check"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'suspended', 'retired')",
            name="edge_nodes_status_check",
        ),
        sa.CheckConstraint("quota_bytes >= 0", name="edge_nodes_quota_check"),
    )
    op.create_index("edge_nodes_last_poll_idx", "edge_nodes", ["last_poll_at"])

    op.create_table(
        "edge_request_nonces",
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("seen_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("node_id", "nonce"),
    )
    op.create_index("edge_request_nonces_seen_idx", "edge_request_nonces", ["seen_at"])

    op.create_table(
        "edge_objects",
        sa.Column("digest", sa.Text(), primary_key=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", _TS, nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "digest ~ '^[0-9a-f]{64}$'", name="edge_objects_digest_check"
        ),
        sa.CheckConstraint("size_bytes > 0", name="edge_objects_size_check"),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoke_reason IS NULL)",
            name="edge_objects_revoke_reason_check",
        ),
    )

    op.create_table(
        "edge_assignments",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("assigned_at", _TS, nullable=False),
        sa.Column("lease_expires_at", _TS, nullable=False),
        sa.Column("fill_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_filled_at", _TS, nullable=True),
        sa.Column("acked_at", _TS, nullable=True),
        sa.Column("released_at", _TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["node_id"], ["edge_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["digest"], ["edge_objects.digest"]),
        sa.CheckConstraint("fill_count >= 0", name="edge_assignments_fill_count_check"),
    )
    op.create_index(
        "edge_assignments_open_key",
        "edge_assignments",
        ["node_id", "digest"],
        unique=True,
        postgresql_where=sa.text("released_at IS NULL"),
    )
    op.create_index("edge_assignments_digest_idx", "edge_assignments", ["digest"])
    op.create_index(
        "edge_assignments_node_idx", "edge_assignments", ["node_id", "released_at"]
    )

    op.create_table(
        "edge_challenge_slots",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("window_index", sa.BigInteger(), nullable=False),
        sa.Column("due_at", _TS, nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="scheduled"),
        sa.Column("challenge_id", sa.Text(), nullable=True, unique=True),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column("digest", sa.Text(), nullable=True),
        sa.Column("range_offset", sa.BigInteger(), nullable=True),
        sa.Column("range_length", sa.Integer(), nullable=True),
        sa.Column("nonce_hex", sa.Text(), nullable=True),
        sa.Column("issued_at", _TS, nullable=True),
        sa.Column("deadline_at", _TS, nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("answered_at", _TS, nullable=True),
        sa.ForeignKeyConstraint(
            ["node_id"], ["edge_nodes.node_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "purpose IN ('possession', 'retrieval', 'revoked')",
            name="edge_challenge_slots_purpose_check",
        ),
        sa.CheckConstraint(
            "state IN ('scheduled', 'issued', 'missed', 'void')",
            name="edge_challenge_slots_state_check",
        ),
        sa.CheckConstraint(
            "outcome IN ('pending', 'passed', 'failed', 'missed', 'refilled', "
            "'refused', 'refused_ok', 'violation', 'void')",
            name="edge_challenge_slots_outcome_check",
        ),
        sa.CheckConstraint(
            "(state = 'issued') = (challenge_id IS NOT NULL)",
            name="edge_challenge_slots_issued_check",
        ),
    )
    op.create_index(
        "edge_challenge_slots_node_window_idx",
        "edge_challenge_slots",
        ["node_id", "window_index"],
    )
    op.create_index(
        "edge_challenge_slots_window_idx", "edge_challenge_slots", ["window_index"]
    )

    op.create_table(
        "edge_window_closures",
        sa.Column("window_index", sa.BigInteger(), primary_key=True),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("node_count", sa.Integer(), nullable=False),
        sa.Column("closed_at", _TS, server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "edge_ledger_windows",
        sa.Column("window_index", sa.BigInteger(), nullable=False),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("scheduled", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("missed", sa.Integer(), nullable=False),
        sa.Column("refilled", sa.Integer(), nullable=False),
        sa.Column("refused", sa.Integer(), nullable=False),
        sa.Column("revocation_checks", sa.Integer(), nullable=False),
        sa.Column("revocation_violations", sa.Integer(), nullable=False),
        sa.Column("held_gib_hours", sa.Float(), nullable=False),
        sa.Column("availability", sa.Float(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("verified_units", sa.Float(), nullable=False),
        sa.Column("hypothetical_share", sa.Float(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column(
            "weight_eligible", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("computed_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("window_index", "node_id"),
        sa.ForeignKeyConstraint(
            ["window_index"], ["edge_window_closures.window_index"]
        ),
        sa.CheckConstraint(
            "weight_eligible = false", name="edge_ledger_windows_shadow_only_check"
        ),
    )
    op.create_index(
        "edge_ledger_windows_node_idx",
        "edge_ledger_windows",
        ["node_id", "window_index"],
    )


def downgrade() -> None:
    op.drop_index("edge_ledger_windows_node_idx", table_name="edge_ledger_windows")
    op.drop_table("edge_ledger_windows")
    op.drop_table("edge_window_closures")
    op.drop_index("edge_challenge_slots_window_idx", table_name="edge_challenge_slots")
    op.drop_index(
        "edge_challenge_slots_node_window_idx", table_name="edge_challenge_slots"
    )
    op.drop_table("edge_challenge_slots")
    op.drop_index("edge_assignments_node_idx", table_name="edge_assignments")
    op.drop_index("edge_assignments_digest_idx", table_name="edge_assignments")
    op.drop_index("edge_assignments_open_key", table_name="edge_assignments")
    op.drop_table("edge_assignments")
    op.drop_table("edge_objects")
    op.drop_index("edge_request_nonces_seen_idx", table_name="edge_request_nonces")
    op.drop_table("edge_request_nonces")
    op.drop_index("edge_nodes_last_poll_idx", table_name="edge_nodes")
    op.drop_table("edge_nodes")
    op.drop_table("edge_lane_state")
    op.drop_table("edge_lane_settings_revisions")
