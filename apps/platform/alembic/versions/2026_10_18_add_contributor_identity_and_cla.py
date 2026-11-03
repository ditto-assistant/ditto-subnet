"""Add verified GitHub link proofs and default-off, versioned CLA consent.

No legal text is seeded. Maintainers must publish approved, pinned text.
"""

import sqlalchemy as sa

from alembic import op

revision = "dc41b7a6e205"
down_revision = "d0250cf21e57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # These are new tables. No existing hot-table backfills or table scans.
    op.create_table(
        "miner_github_link_attempts",
        sa.Column("attempt_id", sa.UUID(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("hotkey", sa.Text(), nullable=False),
        sa.Column("session_id", sa.UUID(), nullable=True),
        sa.Column("launch_hash", sa.Text(), nullable=False),
        sa.Column("state_hash", sa.Text(), nullable=True),
        sa.Column("code_verifier", sa.Text(), nullable=True),
        sa.Column("browser_binding_hash", sa.Text(), nullable=True),
        sa.Column("accept_token_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=True),
        sa.Column("github_login", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["miner_sessions.session_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("launch_hash"),
        sa.UniqueConstraint("state_hash"),
        sa.CheckConstraint(
            "status IN ('pending','identity_verified','accepted',"
            "'linked','failed','expired')",
            name="status",
        ),
    )
    op.create_index(
        "miner_github_link_attempts_hotkey_idx",
        "miner_github_link_attempts",
        ["hotkey", "expires_at"],
    )
    # Bounded pruning: expired open attempts lose their secrets, and old
    # unlinked attempts are deleted. Linked attempts stay as link provenance.
    op.create_index(
        "miner_github_link_attempts_open_expiry_idx",
        "miner_github_link_attempts",
        ["expires_at"],
        postgresql_where=sa.text(
            "status IN ('pending','identity_verified','accepted')"
        ),
    )
    op.create_index(
        "miner_github_link_attempts_closed_created_idx",
        "miner_github_link_attempts",
        ["created_at"],
        postgresql_where=sa.text("status IN ('failed','expired')"),
    )
    op.create_table(
        "miner_github_action_nonces",
        sa.Column("nonce", sa.UUID(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("hotkey", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("key_kind", sa.Text(), nullable=False),
        sa.Column("link_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "miner_github_links",
        sa.Column("link_id", sa.UUID(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("hotkey", sa.Text(), nullable=False),
        sa.Column("coldkey", sa.Text(), nullable=False),
        sa.Column("github_user_id", sa.BigInteger(), nullable=False),
        sa.Column("github_login", sa.Text(), nullable=False),
        sa.Column("link_attempt_id", sa.UUID(), nullable=False),
        sa.Column("nonce", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key_kind", sa.Text(), nullable=False),
        sa.Column("signer", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_block", sa.BigInteger(), nullable=False),
        sa.Column("owner_block_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["link_attempt_id"], ["miner_github_link_attempts.attempt_id"]
        ),
        sa.ForeignKeyConstraint(["nonce"], ["miner_github_action_nonces.nonce"]),
        sa.UniqueConstraint("nonce"),
        sa.CheckConstraint("github_user_id > 0", name="github_id"),
        sa.CheckConstraint("key_kind IN ('hotkey','coldkey')", name="key_kind"),
    )
    # Attempt pruning deletes attempts; this keeps its FK check off a scan.
    op.create_index(
        "miner_github_links_attempt_idx", "miner_github_links", ["link_attempt_id"]
    )
    # Identity is globally one-to-one. A netuid binds the proof, not a second
    # active identity mapping for the same hotkey or GitHub account.
    for field in ("hotkey", "github_user_id"):
        name = "hotkey" if field == "hotkey" else "github"
        op.create_index(
            f"miner_github_links_active_{name}_key",
            "miner_github_links",
            [field],
            unique=True,
            postgresql_where=sa.text("revoked_at IS NULL"),
        )
    op.create_table(
        "miner_github_operator_revocations",
        sa.Column("link_id", sa.UUID(), primary_key=True),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["link_id"], ["miner_github_links.link_id"]),
    )
    op.create_table(
        "cla_versions",
        sa.Column("version", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "version > 0 AND sha256 ~ '^[0-9a-f]{64}$'", name="identity"
        ),
    )
    op.create_table(
        "cla_signatures",
        sa.Column("signature_id", sa.UUID(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("hotkey", sa.Text(), nullable=False),
        sa.Column("coldkey", sa.Text(), nullable=False),
        sa.Column("cla_version", sa.Integer(), nullable=False),
        sa.Column("cla_sha256", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("owner_block", sa.BigInteger(), nullable=False),
        sa.Column("owner_block_hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["cla_version"], ["cla_versions.version"]),
        sa.UniqueConstraint("nonce", name=op.f("uq_cla_signatures_nonce")),
        # One immutable receipt per hotkey and version keeps the history bounded.
        sa.UniqueConstraint(
            "netuid",
            "hotkey",
            "cla_version",
            name=op.f("uq_cla_signatures_hotkey_version"),
        ),
        sa.CheckConstraint("cla_sha256 ~ '^[0-9a-f]{64}$'", name="digest"),
    )
    op.create_table(
        "cla_settings_revisions",
        sa.Column("revision", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("mode IN ('off','shadow','enforce')", name="mode"),
    )
    op.execute(
        "INSERT INTO cla_settings_revisions VALUES "
        "(0, 0, 'off', 'migration', "
        "'Default off until approved legal text and adoption rollout', now())"
    )
    op.create_table(
        "cla_shadow_uploads",
        sa.Column("observation_id", sa.UUID(), primary_key=True),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("hotkey", sa.Text(), nullable=False),
        sa.Column("cla_version", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("cla_shadow_uploads_netuid_idx", "cla_shadow_uploads", ["netuid"])
    op.create_table(
        "agent_cla_bindings",
        sa.Column("agent_id", sa.UUID(), primary_key=True),
        sa.Column("signature_id", sa.UUID(), nullable=False),
        # The binding is the artifact's consent provenance, so like the other
        # artifact audit records it keeps its agent: agents are never deleted.
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["signature_id"], ["cla_signatures.signature_id"], ondelete="RESTRICT"
        ),
    )
    op.execute("""CREATE FUNCTION contributor_identity_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'contributor identity history is append-only'; END $$""")
    for table in (
        "miner_github_action_nonces",
        "miner_github_operator_revocations",
        "cla_versions",
        "cla_signatures",
        "cla_settings_revisions",
        "cla_shadow_uploads",
        "agent_cla_bindings",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION contributor_identity_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION contributor_identity_immutable()"
        )
    op.execute("""CREATE FUNCTION github_link_revocation_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'GitHub link history cannot be deleted';
          END IF;
          IF OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
             OR NEW.revoked_at < OLD.linked_at
             OR (to_jsonb(NEW) - 'revoked_at')
                IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at')
          THEN
            RAISE EXCEPTION 'GitHub proof is immutable; only revocation is allowed';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER miner_github_links_immutable BEFORE UPDATE OR DELETE "
        "ON miner_github_links FOR EACH ROW "
        "EXECUTE FUNCTION github_link_revocation_only()"
    )
    op.execute(
        "CREATE TRIGGER miner_github_links_no_truncate BEFORE TRUNCATE "
        "ON miner_github_links FOR EACH STATEMENT "
        "EXECUTE FUNCTION contributor_identity_immutable()"
    )
    # A linked attempt is the provenance of an immutable link. Only the
    # session FK may still be cleared (ON DELETE SET NULL).
    op.execute("""CREATE FUNCTION github_link_attempt_linked_immutable()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status = 'linked' AND (
            to_jsonb(NEW) - 'session_id' IS DISTINCT FROM to_jsonb(OLD) - 'session_id'
            OR (NEW.session_id IS NOT NULL
                AND NEW.session_id IS DISTINCT FROM OLD.session_id)
          ) THEN
            RAISE EXCEPTION 'linked GitHub attempts are immutable';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER miner_github_link_attempts_linked_immutable "
        "BEFORE UPDATE ON miner_github_link_attempts FOR EACH ROW "
        "EXECUTE FUNCTION github_link_attempt_linked_immutable()"
    )


def downgrade() -> None:
    for table in (
        "agent_cla_bindings",
        "cla_shadow_uploads",
        "cla_settings_revisions",
        "cla_signatures",
        "cla_versions",
        "miner_github_operator_revocations",
        "miner_github_links",
        "miner_github_action_nonces",
        "miner_github_link_attempts",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION contributor_identity_immutable()")
    op.execute("DROP FUNCTION github_link_revocation_only()")
    op.execute("DROP FUNCTION github_link_attempt_linked_immutable()")
