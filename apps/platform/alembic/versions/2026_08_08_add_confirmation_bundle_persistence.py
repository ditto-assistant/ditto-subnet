"""add benchmark v9 confirmation bundle persistence

Revision ID: b4d9e7c2a601
Revises: e2b7c4a1d590
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b4d9e7c2a601"
down_revision: str | Sequence[str] | None = "e2b7c4a1d590"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

    op.create_table(
        "confirmation_bundle_settings_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("settings", json_type, nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("scope = '*'", name="confirmation_settings_scope_check"),
        sa.CheckConstraint(
            "length(checksum) = 64", name="confirmation_settings_checksum_check"
        ),
        sa.CheckConstraint(
            "parent_revision >= 0", name="confirmation_settings_parent_check"
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="confirmation_settings_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="confirmation_settings_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "scope", "parent_revision", name="confirmation_settings_parent_key"
        ),
        sa.UniqueConstraint(
            "revision", "checksum", name="confirmation_settings_revision_checksum_key"
        ),
    )
    op.create_index(
        "confirmation_settings_scope_revision_idx",
        "confirmation_bundle_settings_revisions",
        ["scope", "revision"],
        unique=True,
    )

    # The source bundle foreign key is added after confirmation_bundles exists.
    # Retest authorizations must otherwise exist first because bundles reference
    # their full immutable authorization identity.
    op.create_table(
        "confirmation_retest_authorizations",
        sa.Column("authorization_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("source_bundle_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("profile_revision", sa.Text(), nullable=False),
        sa.Column("profile_checksum", sa.Text(), nullable=False),
        sa.Column("from_generation", sa.Integer(), nullable=False),
        sa.Column("authorized_generation", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64", name="confirmation_retest_sha_check"
        ),
        sa.CheckConstraint(
            "bench_version = 9", name="confirmation_retest_version_check"
        ),
        sa.CheckConstraint(
            "length(profile_revision) BETWEEN 1 AND 128",
            name="confirmation_retest_profile_revision_check",
        ),
        sa.CheckConstraint(
            "length(profile_checksum) = 64",
            name="confirmation_retest_profile_checksum_check",
        ),
        sa.CheckConstraint(
            "from_generation >= 0 AND authorized_generation = from_generation + 1",
            name="confirmation_retest_generation_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8", name="confirmation_retest_reason_check"
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="confirmation_retest_actor_check",
        ),
        sa.PrimaryKeyConstraint("authorization_id"),
        sa.UniqueConstraint(
            "artifact_sha256",
            "bench_version",
            "profile_revision",
            "profile_checksum",
            "authorized_generation",
            name="confirmation_retest_generation_key",
        ),
        sa.UniqueConstraint(
            "authorization_id",
            "artifact_sha256",
            "bench_version",
            "profile_revision",
            "profile_checksum",
            "authorized_generation",
            name="confirmation_retest_bundle_fkey_target",
        ),
    )

    op.create_table(
        "confirmation_bundles",
        sa.Column("bundle_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("profile_revision", sa.Text(), nullable=False),
        sa.Column("profile_checksum", sa.Text(), nullable=False),
        sa.Column("retest_generation", sa.Integer(), nullable=False),
        sa.Column("retest_authorization_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("generation_reason", sa.Text(), nullable=False),
        sa.Column("source_bundle_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("settings_checksum", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("qualification_status", sa.Text(), nullable=True),
        sa.Column("completion_mode", sa.Text(), nullable=True),
        sa.Column("completion_ticket_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_root", json_type, nullable=True),
        sa.Column("evidence_sha256", sa.Text(), nullable=True),
        sa.Column("reporter_hotkey", sa.Text(), nullable=True),
        sa.Column("bundle_signature", sa.Text(), nullable=True),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64", name="confirmation_bundles_sha_check"
        ),
        sa.CheckConstraint(
            "bench_version = 9", name="confirmation_bundles_version_check"
        ),
        sa.CheckConstraint(
            "length(profile_revision) BETWEEN 1 AND 128",
            name="confirmation_bundles_profile_revision_check",
        ),
        sa.CheckConstraint(
            "length(profile_checksum) = 64",
            name="confirmation_bundles_profile_checksum_check",
        ),
        sa.CheckConstraint(
            "length(settings_checksum) = 64",
            name="confirmation_bundles_settings_checksum_check",
        ),
        sa.CheckConstraint(
            "(generation_reason = 'initial' AND retest_generation = 0 "
            "AND retest_authorization_id IS NULL AND source_bundle_id IS NULL) OR "
            "(generation_reason = 'operator_retest' AND retest_generation > 0 "
            "AND retest_authorization_id IS NOT NULL "
            "AND source_bundle_id IS NOT NULL) OR "
            "(generation_reason = 'settings_supersession' "
            "AND retest_generation > 0 AND retest_authorization_id IS NULL "
            "AND source_bundle_id IS NOT NULL)",
            name="confirmation_bundles_generation_auth_check",
        ),
        sa.CheckConstraint(
            "state IN ('blocked_budget', 'pending', 'leased', 'failed', "
            "'completed', 'superseded')",
            name="confirmation_bundles_state_check",
        ),
        sa.CheckConstraint(
            "(state NOT IN ('completed', 'superseded') "
            "AND qualification_status IS NULL AND completion_mode IS NULL "
            "AND completion_ticket_id IS NULL AND evidence_root IS NULL "
            "AND evidence_sha256 IS NULL AND reporter_hotkey IS NULL "
            "AND bundle_signature IS NULL AND verified_at IS NULL "
            "AND completed_at IS NULL) OR "
            "(state IN ('completed', 'superseded') "
            "AND qualification_status IN ('qualified', 'unqualified') "
            "AND completion_mode IN ('shadow', 'enforce') "
            "AND completion_ticket_id IS NOT NULL AND evidence_root IS NOT NULL "
            "AND length(evidence_sha256) = 64 "
            "AND length(trim(reporter_hotkey)) > 0 "
            "AND length(bundle_signature) BETWEEN 2 AND 512 "
            "AND verified_at IS NOT NULL AND completed_at IS NOT NULL) OR "
            "(state = 'superseded' AND qualification_status IS NULL "
            "AND completion_mode IS NULL AND completion_ticket_id IS NULL "
            "AND evidence_root IS NULL AND evidence_sha256 IS NULL "
            "AND reporter_hotkey IS NULL AND bundle_signature IS NULL "
            "AND verified_at IS NULL AND completed_at IS NULL)",
            name="confirmation_bundles_completion_check",
        ),
        sa.ForeignKeyConstraint(
            ["source_bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_bundles_source_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["settings_revision", "settings_checksum"],
            [
                "confirmation_bundle_settings_revisions.revision",
                "confirmation_bundle_settings_revisions.checksum",
            ],
            name="confirmation_bundles_settings_fkey",
        ),
        sa.ForeignKeyConstraint(
            [
                "retest_authorization_id",
                "artifact_sha256",
                "bench_version",
                "profile_revision",
                "profile_checksum",
                "retest_generation",
            ],
            [
                "confirmation_retest_authorizations.authorization_id",
                "confirmation_retest_authorizations.artifact_sha256",
                "confirmation_retest_authorizations.bench_version",
                "confirmation_retest_authorizations.profile_revision",
                "confirmation_retest_authorizations.profile_checksum",
                "confirmation_retest_authorizations.authorized_generation",
            ],
            name="confirmation_bundles_retest_auth_fkey",
        ),
        sa.PrimaryKeyConstraint("bundle_id"),
        sa.UniqueConstraint(
            "artifact_sha256",
            "bench_version",
            "profile_revision",
            "profile_checksum",
            "retest_generation",
            name="confirmation_bundles_identity_key",
        ),
        sa.UniqueConstraint(
            "retest_authorization_id", name="confirmation_bundles_retest_auth_key"
        ),
        sa.UniqueConstraint("source_bundle_id", name="confirmation_bundles_source_key"),
    )
    op.create_index(
        "confirmation_bundles_state_created_idx",
        "confirmation_bundles",
        ["state", "created_at"],
        unique=False,
    )
    op.create_foreign_key(
        "confirmation_retest_source_bundle_fkey",
        "confirmation_retest_authorizations",
        "confirmation_bundles",
        ["source_bundle_id"],
        ["bundle_id"],
    )

    op.create_table(
        "confirmation_bundle_subjects",
        sa.Column("agent_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("bench_version", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("bundle_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("result_status", sa.Text(), nullable=False),
        sa.Column("base_evidence_sha256", sa.Text(), nullable=False),
        sa.Column("base_quality_micros", sa.Integer(), nullable=False),
        sa.Column("base_stderr_micros", sa.Integer(), nullable=False),
        sa.Column("base_model_factor_bps", sa.Integer(), nullable=False),
        sa.Column("base_tool_factor_bps", sa.Integer(), nullable=False),
        sa.Column("full_quality_micros", sa.Integer(), nullable=True),
        sa.Column("full_stderr_micros", sa.Integer(), nullable=True),
        sa.Column("semantic_factor_bps", sa.Integer(), nullable=True),
        sa.Column("applied_factor_bps", sa.Integer(), nullable=True),
        sa.Column("full_effective_micros", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "bench_version = 9", name="confirmation_subjects_version_check"
        ),
        sa.CheckConstraint(
            "length(artifact_sha256) = 64", name="confirmation_subjects_sha_check"
        ),
        sa.CheckConstraint(
            "length(base_evidence_sha256) = 64",
            name="confirmation_subjects_base_evidence_sha_check",
        ),
        sa.CheckConstraint(
            "base_quality_micros BETWEEN 0 AND 1000000 "
            "AND base_stderr_micros BETWEEN 0 AND 1000000",
            name="confirmation_subjects_base_score_check",
        ),
        sa.CheckConstraint(
            "base_model_factor_bps IN (0, 10000) "
            "AND base_tool_factor_bps IN (0, 10000)",
            name="confirmation_subjects_base_factor_check",
        ),
        sa.CheckConstraint(
            "(full_quality_micros IS NULL AND full_stderr_micros IS NULL "
            "AND semantic_factor_bps IS NULL AND applied_factor_bps IS NULL "
            "AND full_effective_micros IS NULL) OR "
            "(full_quality_micros BETWEEN 0 AND 1000000 "
            "AND full_stderr_micros BETWEEN 0 AND 1000000 "
            "AND semantic_factor_bps IN (0, 10000) "
            "AND applied_factor_bps IN (0, 10000) "
            "AND full_effective_micros BETWEEN 0 AND 1000000)",
            name="confirmation_subjects_full_projection_check",
        ),
        sa.CheckConstraint(
            "(result_status = 'base_only' AND bundle_id IS NULL "
            "AND full_quality_micros IS NULL) OR "
            "(result_status = 'provisional' AND bundle_id IS NOT NULL) OR "
            "(result_status = 'full_confirmed' AND bundle_id IS NOT NULL "
            "AND full_quality_micros IS NOT NULL)",
            name="confirmation_subjects_status_bundle_check",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.agent_id"],
            name="confirmation_subjects_agent_fkey",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_subjects_bundle_fkey",
        ),
        sa.PrimaryKeyConstraint(
            "agent_id", "bench_version", name="confirmation_bundle_subjects_pkey"
        ),
    )
    op.create_index(
        "confirmation_subjects_bundle_idx",
        "confirmation_bundle_subjects",
        ["bundle_id"],
        unique=False,
    )
    op.create_index(
        "confirmation_subjects_status_idx",
        "confirmation_bundle_subjects",
        ["result_status"],
        unique=False,
    )

    op.create_table(
        "confirmation_bundle_tickets",
        sa.Column("ticket_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("bundle_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("slot_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("deadline", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("failed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('issued', 'scored', 'expired')",
            name="confirmation_tickets_status_check",
        ),
        sa.CheckConstraint("attempt > 0", name="confirmation_tickets_attempt_check"),
        sa.CheckConstraint(
            "deadline > issued_at", name="confirmation_tickets_deadline_check"
        ),
        sa.CheckConstraint(
            "(status = 'expired') OR (failure_reason IS NULL AND failed_at IS NULL)",
            name="confirmation_tickets_failure_state_check",
        ),
        sa.CheckConstraint(
            "(failure_reason IS NULL) = (failed_at IS NULL)",
            name="confirmation_tickets_failure_pair_check",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_tickets_bundle_fkey",
        ),
        sa.PrimaryKeyConstraint("ticket_id"),
        sa.UniqueConstraint(
            "bundle_id", "attempt", name="confirmation_tickets_attempt_key"
        ),
        sa.UniqueConstraint(
            "ticket_id", "bundle_id", name="confirmation_tickets_id_bundle_key"
        ),
    )
    op.create_index(
        "confirmation_tickets_one_live_bundle_idx",
        "confirmation_bundle_tickets",
        ["bundle_id"],
        unique=True,
        postgresql_where=sa.text("status = 'issued'"),
    )
    op.create_index(
        "confirmation_tickets_open_deadline_idx",
        "confirmation_bundle_tickets",
        ["deadline"],
        unique=False,
        postgresql_where=sa.text("status = 'issued'"),
    )
    op.create_foreign_key(
        "confirmation_bundles_completion_ticket_fkey",
        "confirmation_bundles",
        "confirmation_bundle_tickets",
        ["completion_ticket_id", "bundle_id"],
        ["ticket_id", "bundle_id"],
    )

    op.create_table(
        "confirmation_dimension_evidence",
        sa.Column("bundle_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("dimension", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.Text(), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("provider_cost_microusd", sa.BigInteger(), nullable=False),
        sa.Column("latency_ms", sa.BigInteger(), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "dimension IN ('longmemeval', 'inference_ablation', 'embedding_ablation')",
            name="confirmation_evidence_dimension_check",
        ),
        sa.CheckConstraint(
            "status IN ('completed', 'not_run', 'unavailable')",
            name="confirmation_evidence_status_check",
        ),
        sa.CheckConstraint(
            "request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
            "AND provider_cost_microusd >= 0 AND latency_ms >= 0",
            name="confirmation_evidence_nonnegative_check",
        ),
        sa.CheckConstraint(
            "(dimension = 'longmemeval' AND status = 'completed' "
            "AND synthetic = false) OR "
            "(dimension IN ('inference_ablation', 'embedding_ablation') "
            "AND synthetic = true AND request_count = 0 AND input_tokens = 0 "
            "AND output_tokens = 0 AND provider_cost_microusd = 0)",
            name="confirmation_evidence_synthetic_cost_check",
        ),
        sa.CheckConstraint(
            "length(evidence_sha256) = 64",
            name="confirmation_evidence_sha_check",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_evidence_bundle_fkey",
        ),
        sa.PrimaryKeyConstraint(
            "bundle_id", "dimension", name="confirmation_dimension_evidence_pkey"
        ),
    )

    op.create_table(
        "confirmation_budget_days",
        sa.Column("utc_day", sa.Date(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("issued_attempts", sa.Integer(), nullable=False),
        sa.Column("outstanding_reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("settled_microusd", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision >= 0 AND issued_attempts >= 0 "
            "AND outstanding_reserved_microusd >= 0 AND settled_microusd >= 0",
            name="confirmation_budget_nonnegative_check",
        ),
        sa.PrimaryKeyConstraint("utc_day"),
    )

    op.create_table(
        "confirmation_budget_reservations",
        sa.Column("reservation_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("bundle_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("utc_day", sa.Date(), nullable=False),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("actual_microusd", sa.BigInteger(), nullable=True),
        sa.Column("failed_attempt", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("settled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "attempt > 0 AND reserved_microusd > 0",
            name="confirmation_reservations_positive_check",
        ),
        sa.CheckConstraint(
            "state IN ('reserved', 'settled')",
            name="confirmation_reservations_state_check",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND actual_microusd IS NULL "
            "AND failed_attempt IS NULL AND settled_at IS NULL) OR "
            "(state = 'settled' AND actual_microusd >= 0 "
            "AND failed_attempt IS NOT NULL AND settled_at IS NOT NULL)",
            name="confirmation_reservations_settlement_check",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_id"],
            ["confirmation_bundles.bundle_id"],
            name="confirmation_reservations_bundle_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["utc_day"],
            ["confirmation_budget_days.utc_day"],
            name="confirmation_reservations_day_fkey",
        ),
        sa.ForeignKeyConstraint(
            ["settings_revision"],
            ["confirmation_bundle_settings_revisions.revision"],
            name="confirmation_reservations_settings_fkey",
        ),
        sa.PrimaryKeyConstraint("reservation_id"),
        sa.UniqueConstraint(
            "bundle_id", "attempt", name="confirmation_reservations_attempt_key"
        ),
    )
    op.create_index(
        "confirmation_reservations_one_open_bundle_idx",
        "confirmation_budget_reservations",
        ["bundle_id"],
        unique=True,
        postgresql_where=sa.text("state = 'reserved'"),
    )
    op.create_index(
        "confirmation_reservations_day_idx",
        "confirmation_budget_reservations",
        ["utc_day"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION guard_confirmation_bundle_immutability() RETURNS trigger AS $$
        BEGIN
            IF ROW(
                NEW.bundle_id,
                NEW.artifact_sha256,
                NEW.bench_version,
                NEW.profile_revision,
                NEW.profile_checksum,
                NEW.retest_generation,
                NEW.retest_authorization_id,
                NEW.generation_reason,
                NEW.source_bundle_id,
                NEW.settings_revision,
                NEW.settings_checksum,
                NEW.created_at
            ) IS DISTINCT FROM ROW(
                OLD.bundle_id,
                OLD.artifact_sha256,
                OLD.bench_version,
                OLD.profile_revision,
                OLD.profile_checksum,
                OLD.retest_generation,
                OLD.retest_authorization_id,
                OLD.generation_reason,
                OLD.source_bundle_id,
                OLD.settings_revision,
                OLD.settings_checksum,
                OLD.created_at
            ) THEN
                RAISE EXCEPTION 'confirmation bundle identity is immutable'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'confirmation_bundles_immutability_guard';
            END IF;

            IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
                (OLD.state = 'pending' AND NEW.state IN (
                    'blocked_budget', 'leased', 'superseded'
                )) OR
                (OLD.state = 'blocked_budget' AND NEW.state IN (
                    'pending', 'leased', 'superseded'
                )) OR
                (OLD.state = 'failed' AND NEW.state IN (
                    'blocked_budget', 'leased', 'superseded'
                )) OR
                (OLD.state = 'leased' AND NEW.state IN ('failed', 'completed')) OR
                (OLD.state = 'completed' AND NEW.state = 'superseded')
            ) THEN
                RAISE EXCEPTION 'invalid confirmation bundle state transition'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'confirmation_bundles_immutability_guard';
            END IF;

            IF OLD.evidence_sha256 IS NOT NULL
               AND ROW(
                    NEW.qualification_status,
                    NEW.completion_mode,
                    NEW.completion_ticket_id,
                    NEW.evidence_root,
                    NEW.evidence_sha256,
                    NEW.reporter_hotkey,
                    NEW.bundle_signature,
                    NEW.verified_at,
                    NEW.completed_at
               ) IS DISTINCT FROM ROW(
                    OLD.qualification_status,
                    OLD.completion_mode,
                    OLD.completion_ticket_id,
                    OLD.evidence_root,
                    OLD.evidence_sha256,
                    OLD.reporter_hotkey,
                    OLD.bundle_signature,
                    OLD.verified_at,
                    OLD.completed_at
               ) THEN
                RAISE EXCEPTION 'completed confirmation evidence is immutable'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'confirmation_bundles_immutability_guard';
            END IF;

            IF NEW.state = 'completed' AND OLD.state <> 'completed' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM confirmation_bundle_tickets ticket
                    WHERE ticket.ticket_id = NEW.completion_ticket_id
                      AND ticket.bundle_id = NEW.bundle_id
                      AND ticket.status = 'issued'
                      AND ticket.deadline > NEW.completed_at
                ) THEN
                    RAISE EXCEPTION 'completion requires the exact live ticket'
                        USING ERRCODE = 'check_violation',
                              CONSTRAINT = 'confirmation_bundles_immutability_guard';
                END IF;
                IF (SELECT count(*) FROM confirmation_dimension_evidence evidence
                    WHERE evidence.bundle_id = NEW.bundle_id) <> 3
                   OR NOT EXISTS (
                       SELECT 1 FROM confirmation_dimension_evidence evidence
                       WHERE evidence.bundle_id = NEW.bundle_id
                         AND evidence.dimension = 'longmemeval'
                         AND evidence.status = 'completed'
                   ) THEN
                    RAISE EXCEPTION 'completion requires three typed evidence roots'
                        USING ERRCODE = 'check_violation',
                              CONSTRAINT = 'confirmation_bundles_immutability_guard';
                END IF;
                IF NEW.qualification_status = 'qualified' AND EXISTS (
                    SELECT 1 FROM confirmation_dimension_evidence evidence
                    WHERE evidence.bundle_id = NEW.bundle_id
                      AND evidence.status <> 'completed'
                ) THEN
                    RAISE EXCEPTION 'qualified completion has unavailable evidence'
                        USING ERRCODE = 'check_violation',
                              CONSTRAINT = 'confirmation_bundles_immutability_guard';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER confirmation_bundles_immutability_guard
        BEFORE UPDATE ON confirmation_bundles
        FOR EACH ROW
        EXECUTE FUNCTION guard_confirmation_bundle_immutability()
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_confirmation_dimension_evidence_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'confirmation dimension evidence is append-only'
                USING ERRCODE = 'check_violation',
                      CONSTRAINT = 'confirmation_dimension_evidence_append_only_guard';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER confirmation_dimension_evidence_append_only_guard
        BEFORE UPDATE OR DELETE ON confirmation_dimension_evidence
        FOR EACH ROW
        EXECUTE FUNCTION guard_confirmation_dimension_evidence_append_only()
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_confirmation_subject_authority() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.bundle_id IS NOT NULL AND ROW(
                NEW.artifact_sha256,
                NEW.base_evidence_sha256,
                NEW.base_quality_micros,
                NEW.base_stderr_micros,
                NEW.base_model_factor_bps,
                NEW.base_tool_factor_bps
            ) IS DISTINCT FROM ROW(
                OLD.artifact_sha256,
                OLD.base_evidence_sha256,
                OLD.base_quality_micros,
                OLD.base_stderr_micros,
                OLD.base_model_factor_bps,
                OLD.base_tool_factor_bps
            ) THEN
                RAISE EXCEPTION 'attached confirmation base proof is immutable'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'confirmation_subject_authority_guard';
            END IF;
            IF NEW.result_status = 'full_confirmed' AND NOT EXISTS (
                SELECT 1 FROM confirmation_bundles bundle
                WHERE bundle.bundle_id = NEW.bundle_id
                  AND bundle.artifact_sha256 = NEW.artifact_sha256
                  AND bundle.bench_version = NEW.bench_version
                  AND bundle.state = 'completed'
                  AND bundle.qualification_status = 'qualified'
                  AND (
                      bundle.completion_mode = 'enforce'
                      OR (
                          bundle.completion_mode = 'shadow'
                          AND EXISTS (
                              SELECT 1
                              FROM confirmation_bundle_settings_revisions current
                              WHERE current.scope = '*'
                                AND current.revision = (
                                    SELECT max(latest.revision)
                                    FROM confirmation_bundle_settings_revisions latest
                                    WHERE latest.scope = '*'
                                )
                                AND current.settings ->> 'mode' = 'enforce'
                                AND current.settings ->> 'profile_revision'
                                    = bundle.profile_revision
                                AND current.settings ->> 'profile_checksum'
                                    = bundle.profile_checksum
                          )
                      )
                  )
            ) THEN
                RAISE EXCEPTION 'full confirmation requires qualified enforce evidence'
                    USING ERRCODE = 'check_violation',
                          CONSTRAINT = 'confirmation_subject_authority_guard';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER confirmation_subject_authority_guard
        BEFORE INSERT OR UPDATE ON confirmation_bundle_subjects
        FOR EACH ROW
        EXECUTE FUNCTION guard_confirmation_subject_authority()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS confirmation_subject_authority_guard "
        "ON confirmation_bundle_subjects"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_confirmation_subject_authority()")
    op.execute(
        "DROP TRIGGER IF EXISTS confirmation_dimension_evidence_append_only_guard "
        "ON confirmation_dimension_evidence"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS guard_confirmation_dimension_evidence_append_only()"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS confirmation_bundles_immutability_guard "
        "ON confirmation_bundles"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_confirmation_bundle_immutability()")

    op.drop_index(
        "confirmation_reservations_day_idx",
        table_name="confirmation_budget_reservations",
    )
    op.drop_index(
        "confirmation_reservations_one_open_bundle_idx",
        table_name="confirmation_budget_reservations",
    )
    op.drop_table("confirmation_budget_reservations")
    op.drop_table("confirmation_budget_days")
    op.drop_table("confirmation_dimension_evidence")
    op.drop_constraint(
        "confirmation_bundles_completion_ticket_fkey",
        "confirmation_bundles",
        type_="foreignkey",
    )
    op.drop_index(
        "confirmation_tickets_open_deadline_idx",
        table_name="confirmation_bundle_tickets",
    )
    op.drop_index(
        "confirmation_tickets_one_live_bundle_idx",
        table_name="confirmation_bundle_tickets",
    )
    op.drop_table("confirmation_bundle_tickets")
    op.drop_index(
        "confirmation_subjects_status_idx",
        table_name="confirmation_bundle_subjects",
    )
    op.drop_index(
        "confirmation_subjects_bundle_idx",
        table_name="confirmation_bundle_subjects",
    )
    op.drop_table("confirmation_bundle_subjects")
    op.drop_constraint(
        "confirmation_retest_source_bundle_fkey",
        "confirmation_retest_authorizations",
        type_="foreignkey",
    )
    op.drop_index(
        "confirmation_bundles_state_created_idx",
        table_name="confirmation_bundles",
    )
    op.drop_table("confirmation_bundles")
    op.drop_table("confirmation_retest_authorizations")
    op.drop_index(
        "confirmation_settings_scope_revision_idx",
        table_name="confirmation_bundle_settings_revisions",
    )
    op.drop_table("confirmation_bundle_settings_revisions")
