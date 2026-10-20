"""Allow a report-only public source fixture without a miner submission.

Revision ID: 4c917e2a6b0d
Revises: b6f3d0c7a915
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4c917e2a6b0d"
down_revision: str | Sequence[str] | None = "b6f3d0c7a915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    table = "screener_l2_report_canaries"
    op.add_column(
        table,
        sa.Column(
            "source_kind", sa.Text(), nullable=False, server_default="submission"
        ),
    )
    op.add_column(table, sa.Column("fixture_key", sa.Text(), nullable=True))
    for column, existing_type in (
        ("agent_id", sa.UUID()),
        ("source_attempt_id", sa.UUID()),
        ("expected_agent_status", sa.Text()),
        ("expected_score_count", sa.Integer()),
    ):
        op.alter_column(table, column, existing_type=existing_type, nullable=True)
    op.drop_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_scores_check"),
        table,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_scores_check"),
        table,
        "expected_score_count IS NULL OR expected_score_count >= 0",
    )
    op.drop_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_status_check"),
        table,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_status_check"),
        table,
        "status IN ('awaiting_review', 'ready', 'queued', 'leased', "
        "'succeeded', 'incomplete', 'expired')",
    )
    op.drop_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_label_check"),
        table,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_label_check"),
        table,
        "review_label IN ('unreviewed', 'candidate_clear', 'known_reject')",
    )
    op.create_check_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_source_kind_check"),
        table,
        "(source_kind = 'submission' AND agent_id IS NOT NULL "
        "AND source_attempt_id IS NOT NULL AND fixture_key IS NULL "
        "AND expected_agent_status IS NOT NULL "
        "AND expected_score_count IS NOT NULL) "
        "OR (source_kind = 'canonical_starter_fixture' AND agent_id IS NULL "
        "AND source_attempt_id IS NULL AND fixture_key IS NOT NULL "
        "AND expected_agent_status IS NULL AND expected_score_count IS NULL "
        "AND run_mode = 'source_only')",
    )
    op.create_index(
        "screener_l2_canary_fixture_key_idx",
        table,
        ["fixture_key"],
        unique=True,
        postgresql_where=sa.text("fixture_key IS NOT NULL"),
    )


def downgrade() -> None:
    table = "screener_l2_report_canaries"
    fixture_exists = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM screener_l2_report_canaries "
                "WHERE source_kind = 'canonical_starter_fixture' LIMIT 1"
            )
        )
        .first()
    )
    if fixture_exists is not None:
        raise RuntimeError(
            "Cannot downgrade with a public source fixture present; preserve its "
            "report and operator audit, then remove only that fixture through "
            "a separately reviewed data migration before rolling back."
        )
    op.drop_index("screener_l2_canary_fixture_key_idx", table_name=table)
    op.drop_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_source_kind_check"),
        table,
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_label_check"),
        table,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_label_check"),
        table,
        "review_label IN ('candidate_clear', 'known_reject')",
    )
    op.drop_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_status_check"),
        table,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_status_check"),
        table,
        "status IN ('queued', 'leased', 'succeeded', 'incomplete', 'expired')",
    )
    op.drop_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_scores_check"),
        table,
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_screener_l2_report_canaries_screener_l2_canary_scores_check"),
        table,
        "expected_score_count >= 0",
    )
    for column, existing_type in (
        ("agent_id", sa.UUID()),
        ("source_attempt_id", sa.UUID()),
        ("expected_agent_status", sa.Text()),
        ("expected_score_count", sa.Integer()),
    ):
        op.alter_column(table, column, existing_type=existing_type, nullable=False)
    op.drop_column(table, "fixture_key")
    op.drop_column(table, "source_kind")
