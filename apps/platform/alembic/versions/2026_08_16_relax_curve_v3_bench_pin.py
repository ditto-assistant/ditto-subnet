"""relax the curve-v3 schema pins from bench 9 to bench 9+

Revision ID: c4d9e2f18a67
Revises: 8a31d2c7f4be
Create Date: 2026-08-16

``efficiency.py`` already reads ``bench_version >= BOUNDED_FACTOR_BENCH_VERSION``
so that v10/v11 inherit the bounded-factor tie-break, and efficiency settings
revision 14 activated that fold. The schema installed by ``f2a7c9e41b63`` still
pinned the same rule to ``bench_version = 9`` in three places, so the first
curve-v3 cohort freeze above v9 was rejected by Postgres.

That failure was not confined to scoring: ``GET /api/v1/public/activity`` calls
``ensure_current_efficiency_state`` on the read path, so the ``CheckViolation``
turned the public activity feed — and every operator view built on it — into a
500, and blinded the screener capacity controller that polls it for backlog.

Only the three objects the application can now legitimately violate are relaxed.
``confirmation_scores_efficiency_cost_check`` deliberately keeps its ``= 9``
pin: ``endpoints/validator.py`` still gates signature-bound cost evidence on
``canonical_version == 9``, so relaxing the column constraint would widen a
scoring contract that no writer actually produces yet.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4d9e2f18a67"
down_revision: str | Sequence[str] | None = "8a31d2c7f4be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SNAPSHOT_CONSTRAINT = "efficiency_cohort_snapshots_factor_parameters_check"
_BONUS_CONSTRAINT = "efficiency_bonuses_factor_range_check"


def upgrade() -> None:
    op.drop_constraint(
        _SNAPSHOT_CONSTRAINT, "efficiency_cohort_snapshots", type_="check"
    )
    op.create_check_constraint(
        _SNAPSHOT_CONSTRAINT,
        "efficiency_cohort_snapshots",
        "(curve_version IN (1, 2) AND factor_alpha IS NULL "
        "AND minimum_factor IS NULL AND maximum_factor IS NULL) OR "
        "(curve_version = 3 AND bench_version >= 9 "
        "AND deep_bonus_cap IS NULL AND deep_frontier_ratio IS NULL "
        "AND factor_alpha > 0 AND factor_alpha <= 1 "
        "AND minimum_factor >= 0.85 AND minimum_factor <= 1 "
        "AND maximum_factor >= 1 AND maximum_factor <= 1.1)",
    )

    # The original guard fired on INSERT *and* UPDATE. Widening it to ``>= 9``
    # unchanged would newly reject ordinary lifecycle updates (for example
    # clearing ``active``) on the bench-10 curve-v2 snapshots frozen before the
    # fold was activated. Those legacy rows must stay maintainable, and the
    # first branch already makes every curve-v3 snapshot immutable, so the
    # curve requirement is enforced where it actually matters: creation.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_efficiency_snapshot_curve()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                OLD.curve_version = 3 OR NEW.curve_version = 3
            ) THEN
                RAISE EXCEPTION
                    'curve-v3 efficiency snapshots are immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT'
               AND NEW.bench_version >= 9
               AND NEW.curve_version < 3 THEN
                RAISE EXCEPTION
                    'new bench-v9+ efficiency snapshots require curve v3'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.drop_constraint(_BONUS_CONSTRAINT, "efficiency_bonuses", type_="check")
    op.create_check_constraint(
        _BONUS_CONSTRAINT,
        "efficiency_bonuses",
        "factor IS NULL OR (bench_version >= 9 AND bonus = 0 "
        "AND token_total IS NOT NULL AND token_total > 0 "
        "AND token_total < 'Infinity'::double precision "
        "AND factor >= 0.85 AND factor <= 1.1)",
    )


def downgrade() -> None:
    # Re-pinning to exactly 9 would invalidate any curve-v3 evidence already
    # frozen above v9, and those snapshots and assignments are immutable by
    # construction. Refuse the lossy transition rather than orphan them.
    bind = op.get_bind()
    has_above_v9 = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM efficiency_cohort_snapshots "
            "WHERE curve_version = 3 AND bench_version > 9)"
        )
    ).scalar()
    if has_above_v9:
        raise RuntimeError(
            "cannot re-pin curve-v3 to bench 9 after bench-v10+ snapshots exist"
        )

    op.drop_constraint(_BONUS_CONSTRAINT, "efficiency_bonuses", type_="check")
    op.create_check_constraint(
        _BONUS_CONSTRAINT,
        "efficiency_bonuses",
        "factor IS NULL OR (bench_version = 9 AND bonus = 0 "
        "AND token_total IS NOT NULL AND token_total > 0 "
        "AND token_total < 'Infinity'::double precision "
        "AND factor >= 0.85 AND factor <= 1.1)",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_efficiency_snapshot_curve()
        RETURNS trigger AS $$
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

    op.drop_constraint(
        _SNAPSHOT_CONSTRAINT, "efficiency_cohort_snapshots", type_="check"
    )
    op.create_check_constraint(
        _SNAPSHOT_CONSTRAINT,
        "efficiency_cohort_snapshots",
        "(curve_version IN (1, 2) AND factor_alpha IS NULL "
        "AND minimum_factor IS NULL AND maximum_factor IS NULL) OR "
        "(curve_version = 3 AND bench_version = 9 "
        "AND deep_bonus_cap IS NULL AND deep_frontier_ratio IS NULL "
        "AND factor_alpha > 0 AND factor_alpha <= 1 "
        "AND minimum_factor >= 0.85 AND minimum_factor <= 1 "
        "AND maximum_factor >= 1 AND maximum_factor <= 1.1)",
    )
