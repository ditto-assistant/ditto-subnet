"""Durable administrative intents/outcomes and retained historical audit events."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e804a171db92"
down_revision = "b2af680e139d"
branch_labels = None
depends_on = None

# Fixed source inventory; never discover future tables and publish them implicitly.
_SETTINGS = (
    "copy_court_settings_revisions",
    "core_qualification_policy_revisions",
    "screener_provider_settings_revisions",
    "screener_node_channel_settings_revisions",
    "screener_review_settings_revisions",
    "artifact_release_settings_revisions",
    "submission_settings_revisions",
    "submission_deposit_address_revisions",
    "efficiency_bonus_settings_revisions",
    "continual_retest_settings_revisions",
    "burn_settings_revisions",
    "queue_policy_settings_revisions",
    "inference_concurrency_settings_revisions",
    "validator_slot_settings_revisions",
    "confirmation_bundle_settings_revisions",
    "conversation_settings_revisions",
)


def upgrade() -> None:
    op.create_table(
        "admin_activity",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
    )
    op.create_index(
        "admin_activity_recorded_idx", "admin_activity", ["recorded_at", "id"]
    )
    op.create_table(
        "admin_activity_outcomes",
        sa.Column("activity_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["activity_id"], ["admin_activity.id"]),
        sa.CheckConstraint(
            "status IN ('succeeded', 'failed', 'recorded')",
            name="admin_activity_outcome_status",
        ),
    )
    # Original ledgers remain authoritative and untouched. Historical imports are
    # explicitly 'recorded', not invented HTTP successes. No reason/payload blobs.
    parts = []
    for table in _SETTINGS:
        action = table.removesuffix("_revisions").replace("_", "-")
        parts.append(f"""SELECT created_at AS recorded_at,
            '/api/v1/admin/{action}' AS action, actor,
            jsonb_strip_nulls(jsonb_build_object('revision', to_jsonb(t)->'revision',
                'settings', to_jsonb(t)->'settings',
                'scope', to_jsonb(t)->'scope',
                'cooldown_seconds', to_jsonb(t)->'cooldown_seconds',
                'fee_amount_rao', to_jsonb(t)->'fee_amount_rao',
                'embargo_hours', to_jsonb(t)->'embargo_hours',
                'enabled', to_jsonb(t)->'enabled')) AS details,
            '{table}' AS source FROM {table} t""")
    parts.append("""SELECT recorded_at, '/api/v1/admin/' || event, NULL AS actor,
        jsonb_build_object('agent_id', agent_id, 'canary_id', payload->'canary_id',
        'bench_version', payload->'bench_version'), 'score_audit_log'
        FROM score_audit_log WHERE event IN (
        'benchmark_canary_issued', 'benchmark_canary_cancelled',
        'score_invalidated', 'score_retest_requested')
        OR event LIKE 'benchmark_contract_refresh:%'
        OR event LIKE 'screened_image_rebuild:%'""")
    parts.append("""SELECT recorded_at, '/api/v1/admin/benchmark-rollout/' || event,
        NULL AS actor, jsonb_build_object('rollout_id', rollout_id),
        'benchmark_rollout_audit' FROM benchmark_rollout_audit""")
    for table, action in (
        ("inference_routing_audit", "inference-routing"),
        ("hotkey_ban_audit", "hotkey-ban"),
    ):
        parts.append(f"""SELECT recorded_at,
            '/api/v1/admin/{action}/' || action, actor,
            '{{}}'::jsonb, '{table}' FROM {table}""")
    # Additional durable operator ledgers. Only event identity and public UUIDs
    # are copied; evidence, reasons and private release payloads stay in place.
    for table in (
        "ath_review_actions",
        "screening_retry_overrides",
        "screening_quarantine_resolutions",
        "coding_catalog_releases",
        "coding_hosted_assignments",
        "coding_private_v2_releases",
        "coding_private_v2_release_events",
        "validator_retry_recoveries",
        "validator_queue_withdrawals",
        "validator_queue_reinstatements",
        "submission_retirements",
        "screener_policy_activations",
        "scored_policy_rescreen_releases",
        "scored_screening_snapshot_restorations",
        "confirmation_retest_authorizations",
    ):
        action = table.replace("_", "-")
        parts.append(f"""SELECT created_at,
            '/api/v1/admin/{action}' || coalesce('/' || (to_jsonb(t)->>'action'), ''),
            actor, jsonb_strip_nulls(jsonb_build_object(
                'agent_id', to_jsonb(t)->'agent_id',
                'bench_version', to_jsonb(t)->'bench_version')), '{table}'
            FROM {table} t""")
    op.execute(
        sa.text(
            "INSERT INTO admin_activity "
            "(recorded_at, action, method, actor, details, source) "
            "SELECT recorded_at, action, 'HISTORY', actor, details, source FROM ("
            + " UNION ALL ".join(parts)
            + ") history ORDER BY recorded_at, source, action"
        )
    )
    op.execute(
        "INSERT INTO admin_activity_outcomes (activity_id, recorded_at, status) "
        "SELECT id, recorded_at, 'recorded' FROM admin_activity"
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM admin_activity WHERE source = 'request')")
    ):
        raise RuntimeError("cannot discard administrative activity history")
    op.drop_table("admin_activity_outcomes")
    op.drop_table("admin_activity")
