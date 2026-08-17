"""allow curve-v4 unclamped efficiency factors

Revision ID: a8c4f1d0e92b
Revises: c4d9e2f18a67
Create Date: 2026-08-16

Curve v3 clamps ``(P25 / cost) ** alpha`` to ``[0.85, 1.10]``. Competitive
harnesses saturate the 1.10 cap, so protocol-21 order falls through to
``first_seen``. Curve v4 keeps the same power and the same quality-primary
tie-break, but does not clamp. Frozen v3 snapshots stay on the old
envelope and the old linear headroom transform.

The bonus-row factor check is widened to any finite positive multiplier so
a v4 assignment can persist. The snapshot parameter check gains a v4
branch with the relaxed operator envelope. Factor-curve snapshots (v3 and
v4) stay immutable; new bench-v9+ rows still require a factor curve.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8c4f1d0e92b"
down_revision: str | Sequence[str] | None = "c3f8a1b04e21"
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
        "AND maximum_factor >= 1 AND maximum_factor <= 1.1) OR "
        "(curve_version = 4 AND bench_version >= 9 "
        "AND deep_bonus_cap IS NULL AND deep_frontier_ratio IS NULL "
        "AND factor_alpha > 0 AND factor_alpha <= 1 "
        "AND minimum_factor > 0 AND minimum_factor <= 1 "
        "AND maximum_factor >= 1 AND maximum_factor <= 100 "
        "AND minimum_factor <= maximum_factor)",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_efficiency_snapshot_curve()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                OLD.curve_version >= 3 OR NEW.curve_version >= 3
            ) THEN
                RAISE EXCEPTION
                    'factor-curve efficiency snapshots are immutable'
                    USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT'
               AND NEW.bench_version >= 9
               AND NEW.curve_version < 3 THEN
                RAISE EXCEPTION
                    'new bench-v9+ efficiency snapshots require a factor curve'
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
        "AND factor > 0 AND factor < 'Infinity'::double precision)",
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_efficiency_assignment_curve()
        RETURNS trigger AS $$
        DECLARE
            snapshot_curve integer;
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                OLD.agent_id IS DISTINCT FROM NEW.agent_id
                OR OLD.snapshot_id IS DISTINCT FROM NEW.snapshot_id
                OR OLD.bench_version IS DISTINCT FROM NEW.bench_version
                OR OLD.epoch_index IS DISTINCT FROM NEW.epoch_index
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

            IF snapshot_curve IN (3, 4) THEN
                IF TG_OP = 'INSERT' AND NEW.factor IS NULL THEN
                    NEW.bonus := 0;
                ELSIF TG_OP = 'UPDATE' AND OLD.factor IS NOT NULL THEN
                    RAISE EXCEPTION
                        'authoritative efficiency assignment is immutable'
                        USING ERRCODE = '23514';
                ELSIF TG_OP = 'UPDATE' AND NEW.factor IS NOT NULL THEN
                    IF OLD.bonus <> 0 OR NEW.bonus <> 0 THEN
                        RAISE EXCEPTION
                            'only a neutral factor-curve placeholder may be promoted'
                            USING ERRCODE = '23514';
                    END IF;
                ELSIF TG_OP = 'UPDATE' THEN
                    NEW.bonus := 0;
                END IF;
            ELSIF snapshot_curve IS NOT NULL AND NEW.factor IS NOT NULL THEN
                RAISE EXCEPTION
                    'efficiency factor requires a factor-curve snapshot'
                    USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    has_v4 = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM efficiency_cohort_snapshots "
            "WHERE curve_version = 4)"
        )
    ).scalar()
    if has_v4:
        raise RuntimeError("cannot re-pin factor bounds after curve-v4 snapshots exist")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION guard_efficiency_assignment_curve()
        RETURNS trigger AS $$
        DECLARE
            snapshot_curve integer;
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                OLD.agent_id IS DISTINCT FROM NEW.agent_id
                OR OLD.snapshot_id IS DISTINCT FROM NEW.snapshot_id
                OR OLD.bench_version IS DISTINCT FROM NEW.bench_version
                OR OLD.epoch_index IS DISTINCT FROM NEW.epoch_index
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

    op.drop_constraint(_BONUS_CONSTRAINT, "efficiency_bonuses", type_="check")
    op.create_check_constraint(
        _BONUS_CONSTRAINT,
        "efficiency_bonuses",
        "factor IS NULL OR (bench_version >= 9 AND bonus = 0 "
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
