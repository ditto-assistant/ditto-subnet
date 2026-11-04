"""allow the skipped generator round status

Revision ID: 4c1e7a9d2b58
Revises: 6b3f9d2e8a17
Create Date: 2026-11-04

A round whose frozen columns and panel would only repeat the last scored
round is ``skipped`` when it opens (``skip_unchanged_rounds``): no generation,
no panel runs, no inference. Its ``generation.skipped_unchanged`` names the
scored round the ledger keeps paying. A skipped round keeps its seed (it
opened), so the seed check already admits it, and it has no ``void_reason``.

``generator_rounds`` holds a few hundred rows and is written once a minute by
the round driver: the CHECK is swapped in one ``ALTER TABLE`` with ``NOT
VALID`` and validated afterwards. Re-runnable from any point.
"""

from collections.abc import Sequence

from alembic import op
from ditto.db.migration_lock import run_with_retry

revision: str = "4c1e7a9d2b58"
down_revision: str | Sequence[str] | None = "6b3f9d2e8a17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_generator_rounds_generator_rounds_status_check"
_WITH_SKIPPED = (
    "status IN ('committed', 'generating', 'running', 'scoring', 'scored', "
    "'skipped', 'void')"
)
_WITHOUT_SKIPPED = (
    "status IN ('committed', 'generating', 'running', 'scoring', 'scored', 'void')"
)


def _swap(check_sql: str, label: str) -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        run_with_retry(
            bind,
            [
                "ALTER TABLE generator_rounds "
                f"DROP CONSTRAINT IF EXISTS {_CHECK}, "
                f"ADD CONSTRAINT {_CHECK} CHECK ({check_sql}) NOT VALID"
            ],
            label,
        )
        run_with_retry(
            bind,
            [f"ALTER TABLE generator_rounds VALIDATE CONSTRAINT {_CHECK}"],
            f"validate {label}",
        )


def upgrade() -> None:
    _swap(_WITH_SKIPPED, "allow skipped generator rounds")


def downgrade() -> None:
    # A skipped round cannot be expressed without the status: it never ran,
    # so it reads as void on the way down.
    op.execute(
        "UPDATE generator_rounds SET status = 'void', "
        "void_reason = 'skipped_unchanged (downgraded)' WHERE status = 'skipped'"
    )
    _swap(_WITHOUT_SKIPPED, "disallow skipped generator rounds")
