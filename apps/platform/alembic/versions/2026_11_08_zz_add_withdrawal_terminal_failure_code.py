"""Record why a queue withdrawal was terminal for the artifact.

``terminal_failure_code`` is set when every remaining quorum slot of a
submission died on the same named agent-attributable failure (for example
``inference_allowance_exhausted``). The platform writes it automatically once
quorum becomes unreachable, and the operator withdraw route stamps it when the
same evidence holds. Public surfaces read it to report ``scoring_failed``
instead of a misleading "waiting for validators" or a generic ``not_queued``.

Nullable and metadata-only: ``validator_queue_withdrawals`` is not a hot table
and existing rows simply carry no code.
"""

import sqlalchemy as sa

from alembic import op

revision = "e5a91c3d7b60"
down_revision = "c4d8e2f1a937"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "validator_queue_withdrawals",
        sa.Column("terminal_failure_code", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "validator_queue_withdrawals_terminal_failure_code_length",
        "validator_queue_withdrawals",
        "terminal_failure_code IS NULL "
        "OR length(trim(terminal_failure_code)) BETWEEN 1 AND 120",
    )


def downgrade() -> None:
    op.drop_constraint(
        "validator_queue_withdrawals_terminal_failure_code_length",
        "validator_queue_withdrawals",
        type_="check",
    )
    op.drop_column("validator_queue_withdrawals", "terminal_failure_code")
