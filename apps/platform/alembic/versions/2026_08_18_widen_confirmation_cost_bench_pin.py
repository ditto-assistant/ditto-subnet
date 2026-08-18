"""widen confirmation efficiency-cost bench pin from bench 9 to bench 9+

``efficiency.py`` already averages continual-retest cost through curve v4 for
every ``bench_version >= 9`` cohort, but ``2026_08_16_relax_curve_v3_bench_pin``
(#885) deliberately kept one object pinned at ``bench_version = 9``:
``confirmation_scores_efficiency_cost_check``. Its stated reason was that
``endpoints/validator.py`` still gated signature-bound cost evidence on
``canonical_version == 9``, so relaxing the column would have widened a scoring
contract no writer produced.

Bench v12 is now the active epoch and carries the same signed v9 base-evidence
stack (``V9EvidenceBenchVersion``/``CONFIRMATION_BENCH_VERSIONS`` == 9,10,11,12),
so ``audited_v9_run_token_total`` can mint the same integrity-qualified cost for
a v12 continual retest. The validator gate is widened to
``supports_confirmation(canonical_version)`` in the same change, so this relaxes
the column exactly as far as a real writer now produces. Matches the sibling
curve-v3 constraints that #885 already moved to ``>= 9``; membership is enforced
by the writer, not by the schema.

Additive only: v9 cost rows already written stay valid, and the three
non-cost-eligible branches are untouched.

Revision ID: d5f1a3b62e94
Revises: b7e2c9a14d80
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5f1a3b62e94"
down_revision: str | Sequence[str] | None = "b7e2c9a14d80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CONSTRAINT = "confirmation_scores_efficiency_cost_check"
_TABLE = "confirmation_scores"

_ELIGIBLE_PINNED_9 = (
    "(v9_efficiency_cost_eligible = true "
    "AND bench_version = 9 AND v9_efficiency_token_total > 0)"
)
_ELIGIBLE_AT_LEAST_9 = (
    "(v9_efficiency_cost_eligible = true "
    "AND bench_version >= 9 AND v9_efficiency_token_total > 0)"
)
_NON_ELIGIBLE_BRANCHES = (
    "(v9_efficiency_cost_eligible IS NULL "
    "AND v9_efficiency_token_total IS NULL) OR "
    "(v9_efficiency_cost_eligible = false "
    "AND v9_efficiency_token_total IS NULL) OR "
)


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        _NON_ELIGIBLE_BRANCHES + _ELIGIBLE_AT_LEAST_9,
    )


def downgrade() -> None:
    # Re-pinning to exactly 9 would invalidate any bench-v10+ continual-retest
    # cost evidence already appended, and the confirmation ledger is immutable
    # by construction. Refuse the lossy transition rather than orphan those rows.
    bind = op.get_bind()
    has_above_v9 = bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM confirmation_scores "
            "WHERE v9_efficiency_cost_eligible = true AND bench_version > 9)"
        )
    ).scalar()
    if has_above_v9:
        raise RuntimeError(
            "cannot re-pin confirmation cost to bench 9 after bench-v10+ "
            "cost-eligible rows exist"
        )

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        _NON_ELIGIBLE_BRANCHES + _ELIGIBLE_PINNED_9,
    )
