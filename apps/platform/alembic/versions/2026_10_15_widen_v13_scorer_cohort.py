"""Let a V13 scorer cohort pin name three to sixteen validators.

The per-agent scoring quorum stays three distinct validators (the ticket
primary key is unchanged); only the pinned membership that may claim those
seats widens. Pins and rotations stay append-only and immutable.

Revision ID: 15b4db69f2ce
Revises: ac176f7e64fc
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "15b4db69f2ce"
down_revision: str | Sequence[str] | None = "ac176f7e64fc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("v13_scorer_cohort_pins", "v13_scorer_cohort_rotations")
_WIDE = (
    "jsonb_typeof(hotkeys) = 'array' AND jsonb_array_length(hotkeys) BETWEEN 3 AND 16"
)
_EXACT = "jsonb_typeof(hotkeys) = 'array' AND jsonb_array_length(hotkeys) = 3"


def _replace_hotkey_count_checks(expression: str) -> None:
    """Replace each membership-size check in place, keeping its deployed name.

    The rotation table's name was truncated and hashed by the naming
    convention, so inspect the live constraint instead of guessing either
    spelling, and fail closed if the schema has drifted.
    """
    inspector = sa.inspect(op.get_bind())
    for table_name in _TABLES:
        matches = [
            constraint
            for constraint in inspector.get_check_constraints(table_name)
            if constraint["name"]
            and "jsonb_array_length(hotkeys)" in constraint["sqltext"]
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one hotkey count check on {table_name}, found {len(matches)}"
            )
        name = matches[0]["name"]
        if name is None:
            raise RuntimeError(f"hotkey count check on {table_name} is unnamed")
        op.drop_constraint(op.f(name), table_name, type_="check")
        op.create_check_constraint(op.f(name), table_name, expression)


def upgrade() -> None:
    _replace_hotkey_count_checks(_WIDE)


def downgrade() -> None:
    # Rows are immutable, so a wider cohort cannot be rewritten to fit. Refuse
    # loudly instead of letting the constraint rebuild fail mid-downgrade.
    op.execute(
        """DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM v13_scorer_cohort_pins
              WHERE jsonb_array_length(hotkeys) <> 3
            UNION ALL
            SELECT 1 FROM v13_scorer_cohort_rotations
              WHERE jsonb_array_length(hotkeys) <> 3
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade: a V13 scorer cohort names other than three members';
          END IF;
        END $$"""
    )
    _replace_hotkey_count_checks(_EXACT)
