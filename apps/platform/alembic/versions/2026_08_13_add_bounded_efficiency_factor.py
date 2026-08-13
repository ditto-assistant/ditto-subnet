"""add bounded token-efficiency factor persistence

Revision ID: f2a7c9e41b63
Revises: b7c4e1a90d52
Create Date: 2026-08-13

Curve v3 stores its exponent and multiplier bounds on the immutable cohort
snapshot, then stores the resulting multiplier beside each immutable agent
assignment. Protocol-19 continual-retest rows additionally persist their
signature-bound v9 token cost so each new epoch can use the arithmetic mean of
the canonical quorum and all comparable retests. The existing non-null
``bonus`` column remains unchanged so v1/v2 snapshots replay exactly and older
readers treat v3 rows (whose bonus is zero) as neutral rather than applying an
unbounded or duplicate adjustment.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2a7c9e41b63"
down_revision: str | Sequence[str] | None = "b7c4e1a90d52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Protocol 19 binds the representative v9 base-evidence digest into each
    # new single-seed continual-retest signature. Persist its audited cost (or
    # an explicit invalid marker) so the next daily snapshot can average the
    # initial quorum and all comparable accepted retests. Historical rows stay
    # null and therefore never masquerade as signed cost authority.
    op.add_column(
        "confirmation_scores",
        sa.Column("v9_efficiency_token_total", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "confirmation_scores",
        sa.Column("v9_efficiency_cost_eligible", sa.Boolean(), nullable=True),
    )
    op.create_check_constraint(
        "confirmation_scores_efficiency_cost_check",
        "confirmation_scores",
        "(v9_efficiency_cost_eligible IS NULL "
        "AND v9_efficiency_token_total IS NULL) OR "
        "(v9_efficiency_cost_eligible = false "
        "AND v9_efficiency_token_total IS NULL) OR "
        "(v9_efficiency_cost_eligible = true AND bench_version = 9 "
        "AND v9_efficiency_token_total > 0)",
    )

    # ``snapshot_id`` remains a standalone PK/FK for compatibility. The
    # redundant unique key gives Postgres an exact composite target so new
    # assignments cannot pair that id with another benchmark or epoch. NOT
    # VALID avoids an invasive scan/rewrite of append-only historical rows
    # while still enforcing the FK for every row written after this migration.
    op.create_unique_constraint(
        "efficiency_cohort_snapshots_assignment_identity_key",
        "efficiency_cohort_snapshots",
        ["snapshot_id", "bench_version", "epoch_index"],
    )
    op.create_foreign_key(
        "efficiency_bonuses_snapshot_identity_fkey",
        "efficiency_bonuses",
        "efficiency_cohort_snapshots",
        ["snapshot_id", "bench_version", "epoch_index"],
        ["snapshot_id", "bench_version", "epoch_index"],
        postgresql_not_valid=True,
    )

    op.add_column(
        "efficiency_cohort_snapshots",
        sa.Column("factor_alpha", sa.Float(), nullable=True),
    )
    op.add_column(
        "efficiency_cohort_snapshots",
        sa.Column("minimum_factor", sa.Float(), nullable=True),
    )
    op.add_column(
        "efficiency_cohort_snapshots",
        sa.Column("maximum_factor", sa.Float(), nullable=True),
    )
    op.create_check_constraint(
        "efficiency_cohort_snapshots_factor_parameters_check",
        "efficiency_cohort_snapshots",
        "(curve_version IN (1, 2) AND factor_alpha IS NULL "
        "AND minimum_factor IS NULL AND maximum_factor IS NULL) OR "
        "(curve_version = 3 AND bench_version = 9 "
        "AND deep_bonus_cap IS NULL AND deep_frontier_ratio IS NULL "
        "AND factor_alpha > 0 AND factor_alpha <= 1 "
        "AND minimum_factor >= 0.85 AND minimum_factor <= 1 "
        "AND maximum_factor >= 1 AND maximum_factor <= 1.1)",
    )
    # Preserve already-frozen historical v9 curve-v1/v2 snapshots, but prevent
    # the immediately previous binary from creating another one after this
    # schema is installed. Its v9 inputs are not the typed, signature-bound v3
    # authority, so guessing v3 knobs or silently freezing its legacy P25 would
    # be worse than failing closed during rollback.
    op.execute(
        """
        CREATE FUNCTION guard_efficiency_snapshot_curve() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                OLD.curve_version = 3 OR NEW.curve_version = 3
            ) THEN
                RAISE EXCEPTION
                    'curve-v3 efficiency snapshots are immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF NEW.bench_version = 9 AND NEW.curve_version < 3 THEN
                RAISE EXCEPTION
                    'new bench-v9 efficiency snapshots require curve v3'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER efficiency_cohort_snapshots_curve_guard
        BEFORE INSERT OR UPDATE ON efficiency_cohort_snapshots
        FOR EACH ROW EXECUTE FUNCTION guard_efficiency_snapshot_curve()
        """
    )

    op.add_column(
        "efficiency_bonuses",
        sa.Column("factor", sa.Float(), nullable=True),
    )
    op.create_check_constraint(
        "efficiency_bonuses_factor_range_check",
        "efficiency_bonuses",
        "factor IS NULL OR (bench_version = 9 AND bonus = 0 "
        "AND token_total IS NOT NULL AND token_total > 0 "
        "AND token_total < 'Infinity'::double precision "
        "AND factor >= 0.85 AND factor <= 1.1)",
    )
    # The deploy contract keeps the previous application revision running for
    # a short window after migrations, and an operator rollback rewinds code
    # without rewinding schema. That old writer interprets every curve >= 2 as
    # the legacy upside curve and omits the new ``factor`` column. Force such a
    # write neutral against a v3 snapshot; the new app can later promote that
    # explicit compatibility placeholder from signature-bound v9 evidence.
    # Conversely, never allow a factor to be attached to a legacy snapshot.
    op.execute(
        """
        CREATE FUNCTION guard_efficiency_assignment_curve() RETURNS trigger AS $$
        DECLARE
            snapshot_curve integer;
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.agent_id IS DISTINCT FROM OLD.agent_id OR
                NEW.snapshot_id IS DISTINCT FROM OLD.snapshot_id OR
                NEW.bench_version IS DISTINCT FROM OLD.bench_version OR
                NEW.epoch_index IS DISTINCT FROM OLD.epoch_index
            ) THEN
                RAISE EXCEPTION
                    'efficiency assignment identity is immutable'
                    USING ERRCODE = '23514';
            END IF;

            SELECT curve_version INTO snapshot_curve
              FROM public.efficiency_cohort_snapshots
             WHERE snapshot_id = NEW.snapshot_id
               AND bench_version = NEW.bench_version
               AND epoch_index = NEW.epoch_index;

            IF snapshot_curve = 3 THEN
                IF TG_OP = 'INSERT' AND NEW.factor IS NULL THEN
                    NEW.bonus := 0;
                ELSIF TG_OP = 'UPDATE' AND OLD.factor IS NOT NULL THEN
                    RAISE EXCEPTION
                        'authoritative efficiency assignment is immutable'
                        USING ERRCODE = '23514';
                ELSIF TG_OP = 'UPDATE' AND NEW.factor IS NOT NULL THEN
                    IF OLD.bonus <> 0 OR NEW.bonus <> 0 THEN
                        RAISE EXCEPTION
                            'only a neutral curve-v3 placeholder may be promoted'
                            USING ERRCODE = '23514';
                    END IF;
                ELSIF TG_OP = 'UPDATE' THEN
                    -- A previous binary may retry a neutral write while rolled
                    -- back. It may refresh non-authoritative telemetry, but it
                    -- can never create scoring authority without ``factor``.
                    NEW.bonus := 0;
                END IF;
            ELSIF snapshot_curve IS NOT NULL AND NEW.factor IS NOT NULL THEN
                RAISE EXCEPTION
                    'bounded efficiency factor requires a curve-v3 snapshot'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER efficiency_bonuses_curve_guard
        BEFORE INSERT OR UPDATE OF
            agent_id, snapshot_id, bench_version, epoch_index,
            token_total, bonus, factor
        ON efficiency_bonuses
        FOR EACH ROW EXECUTE FUNCTION guard_efficiency_assignment_curve()
        """
    )


def downgrade() -> None:
    # Removing the multiplier/knobs after v3 assignments exist would silently
    # turn those immutable scores neutral and leave curve_version=3 snapshots
    # for an old app to misread as the two-tier bonus curve. Refuse that lossy
    # transition; operators must roll back before activating/materializing v3.
    has_v3 = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM efficiency_cohort_snapshots "
                "WHERE curve_version = 3)"
            )
        )
        .scalar()
    )
    if has_v3:
        raise RuntimeError(
            "cannot downgrade bounded efficiency schema after curve-v3 snapshots exist"
        )
    op.execute(
        "DROP TRIGGER IF EXISTS efficiency_bonuses_curve_guard ON efficiency_bonuses"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_efficiency_assignment_curve()")
    op.execute(
        "DROP TRIGGER IF EXISTS efficiency_cohort_snapshots_curve_guard "
        "ON efficiency_cohort_snapshots"
    )
    op.execute("DROP FUNCTION IF EXISTS guard_efficiency_snapshot_curve()")
    op.drop_constraint(
        "efficiency_bonuses_factor_range_check",
        "efficiency_bonuses",
        type_="check",
    )
    op.drop_column("efficiency_bonuses", "factor")

    op.drop_constraint(
        "efficiency_cohort_snapshots_factor_parameters_check",
        "efficiency_cohort_snapshots",
        type_="check",
    )
    op.drop_column("efficiency_cohort_snapshots", "maximum_factor")
    op.drop_column("efficiency_cohort_snapshots", "minimum_factor")
    op.drop_column("efficiency_cohort_snapshots", "factor_alpha")

    op.drop_constraint(
        "efficiency_bonuses_snapshot_identity_fkey",
        "efficiency_bonuses",
        type_="foreignkey",
    )
    op.drop_constraint(
        "efficiency_cohort_snapshots_assignment_identity_key",
        "efficiency_cohort_snapshots",
        type_="unique",
    )
    op.drop_constraint(
        "confirmation_scores_efficiency_cost_check",
        "confirmation_scores",
        type_="check",
    )
    op.drop_column("confirmation_scores", "v9_efficiency_cost_eligible")
    op.drop_column("confirmation_scores", "v9_efficiency_token_total")
