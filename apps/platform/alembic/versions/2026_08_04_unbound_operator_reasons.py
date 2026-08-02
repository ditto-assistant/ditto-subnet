"""Remove upper bounds from operator audit reasons.

Revision ID: c0e1f2a3b4d5
Revises: b9d0e1f2a3c4
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c0e1f2a3b4d5"
down_revision: str | Sequence[str] | None = "b9d0e1f2a3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REASON_CONSTRAINTS = (
    ("artifact_release_settings_revisions", 8),
    ("continual_retest_settings_revisions", 8),
    ("efficiency_bonus_settings_revisions", 8),
    ("inference_concurrency_settings_revisions", 8),
    ("queue_policy_settings_revisions", 8),
    ("screener_review_settings_revisions", 8),
    ("screening_retry_overrides", 8),
    ("submission_retirements", 8),
    ("submission_settings_revisions", 8),
    ("validator_queue_reinstatements", 8),
    ("validator_queue_withdrawals", 8),
    ("validator_retry_recoveries", 3),
    ("validator_slot_settings_revisions", 8),
)


def _replace_reason_constraints(*, maximum: int | None) -> None:
    """Replace each live constraint while preserving its deployed name.

    Constraint names differ between raw-SQL legacy tables and tables created
    through SQLAlchemy's naming convention. Inspecting the database avoids
    guessing either spelling and fails closed if the schema has drifted.
    """

    inspector = sa.inspect(op.get_bind())
    for table_name, minimum in _REASON_CONSTRAINTS:
        matches = [
            constraint
            for constraint in inspector.get_check_constraints(table_name)
            if constraint["name"]
            and "trim" in constraint["sqltext"].lower()
            and "reason" in constraint["sqltext"].lower()
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected one reason check on {table_name}, found {len(matches)}"
            )
        name = matches[0]["name"]
        if name is None:
            raise RuntimeError(f"reason check on {table_name} is unnamed")
        op.drop_constraint(op.f(name), table_name, type_="check")
        expression = f"length(trim(reason)) >= {minimum}"
        if maximum is not None:
            expression = f"length(trim(reason)) BETWEEN {minimum} AND {maximum}"
        op.create_check_constraint(op.f(name), table_name, expression)


def upgrade() -> None:
    _replace_reason_constraints(maximum=None)


def downgrade() -> None:
    _replace_reason_constraints(maximum=500)
