"""Generator frontier cases: oracle attestations and sampled operator audits.

A scored round whose panel left cases unsolved can have them attested valid
by the frontier oracle (a subnet-run reference harness answering from a
bounded context) and a sampled operator audit; attested cases then score as
``frontier`` instead of ``unsolved``. The round's frontier lifecycle (oracle
results, attestations, audits, the pre-frontier column scores) lives in one
JSON document on the round. Oracle leases share the round attempt log, marked
by ``kind``.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e5a1f7c3b902"
down_revision = "c4d8e2f1a937"
branch_labels = None
depends_on = None

_VERDICT_STATUSES = (
    "'discriminating', 'trivial', 'unsolved', 'duplicate', 'insufficient_panel'"
)


def upgrade() -> None:
    op.add_column(
        "generator_rounds",
        sa.Column("frontier", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "generator_round_attempts",
        sa.Column("kind", sa.Text(), nullable=False, server_default="panel"),
    )
    op.create_check_constraint(
        "generator_round_attempts_kind_check",
        "generator_round_attempts",
        "kind IN ('panel', 'oracle')",
    )
    op.drop_constraint(
        "generator_round_case_verdicts_status_check",
        "generator_round_case_verdicts",
        type_="check",
    )
    op.create_check_constraint(
        "generator_round_case_verdicts_status_check",
        "generator_round_case_verdicts",
        f"status IN ({_VERDICT_STATUSES}, 'frontier')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE generator_round_case_verdicts SET status = 'unsolved', credit = 0 "
        "WHERE status = 'frontier'"
    )
    op.drop_constraint(
        "generator_round_case_verdicts_status_check",
        "generator_round_case_verdicts",
        type_="check",
    )
    op.create_check_constraint(
        "generator_round_case_verdicts_status_check",
        "generator_round_case_verdicts",
        f"status IN ({_VERDICT_STATUSES})",
    )
    op.drop_constraint(
        "generator_round_attempts_kind_check",
        "generator_round_attempts",
        type_="check",
    )
    op.drop_column("generator_round_attempts", "kind")
    op.drop_column("generator_rounds", "frontier")
