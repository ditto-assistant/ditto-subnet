"""let a v16 dueling season battery pass a policy-14 generator

Operator decision (2026-11-04): from bench v16 on, generator rounds and
dueling seasons accept three pack grading policies
(``challenge.GradingPolicyWindowV16``), so a v16 season accepts packs
declaring 14, 15 or 16. Policies 15 and 16 add no pack validation rule over 14,
so a policy-14 pack validates identically and grades under its declared v14
floor. The battery's ``passed`` floor moves from 15 to 14 to match; the window
itself stays in code (``accepted_grading_versions``), the check is only a
floor.

The constraint name was truncated and hashed by the naming convention, so the
live constraint is inspected rather than guessed, and the migration fails
closed if the schema has drifted. ``dueling_season_batteries`` is small and
off the request hot path, so a validating rebuild is fine.

Revision ID: 4b7e1c9a2f60
Revises: d9e291eb6ec3
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4b7e1c9a2f60"
down_revision: str | Sequence[str] | None = "d9e291eb6ec3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "dueling_season_batteries"
_POLICY_14 = "status <> 'passed' OR grading_bench_version >= 14"
_POLICY_15 = "status <> 'passed' OR grading_bench_version >= 15"


def _replace_policy_floor(expression: str) -> None:
    inspector = sa.inspect(op.get_bind())
    matches = [
        constraint
        for constraint in inspector.get_check_constraints(_TABLE)
        if constraint["name"] and "grading_bench_version" in constraint["sqltext"]
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one grading policy check on {_TABLE}, found {len(matches)}"
        )
    name = matches[0]["name"]
    if name is None:
        raise RuntimeError(f"grading policy check on {_TABLE} is unnamed")
    op.drop_constraint(op.f(name), _TABLE, type_="check")
    op.create_check_constraint(op.f(name), _TABLE, expression)


def upgrade() -> None:
    _replace_policy_floor(_POLICY_14)


def downgrade() -> None:
    # A passed policy-14 battery cannot be rewritten to 15. Refuse loudly
    # instead of letting the constraint rebuild fail mid-downgrade.
    op.execute(
        """DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM dueling_season_batteries
              WHERE status = 'passed' AND grading_bench_version < 15
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade: a dueling season battery passed below policy 15';
          END IF;
        END $$"""
    )
    _replace_policy_floor(_POLICY_15)
