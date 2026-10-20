"""Add bounty evaluation records: estimates, actuals and pilot decisions.

A bounty's evaluation (#2944) compares what its build was estimated to cost
with what it cost, and records the operators' decision to expand, revise or
stop the pilot together with how far the work was integrated. Both are
append-only histories: each new entry for a bounty's kind and metric, or each
new decision, supersedes the one before without erasing it.
"""

import sqlalchemy as sa

from alembic import op

revision = "5de80b078e1c"
down_revision = "e1b8c4f7a209"
branch_labels = None
depends_on = None

KINDS = ("estimate", "actual")
METRICS = (
    "build_hours",
    "reviewer_hours",
    "integration_hours",
    "operator_hours",
    "turnaround_days",
)
DECISIONS = ("expand", "revise", "stop")
INTEGRATION_STATUSES = (
    "not_integrated",
    "integrating",
    "integrated",
    "adopted",
    "not_adopted",
)
# The largest value a metric records; the bound also refuses NaN and infinity,
# which PostgreSQL orders above every number.
MAX_VALUE = 1_000_000
APPEND_ONLY = ("bounty_evaluation_entries", "bounty_evaluation_decisions")


def _quoted(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "bounty_evaluation_entries",
        sa.Column(
            "entry_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True
        ),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bounty_id"], ["bounties.bounty_id"]),
        sa.CheckConstraint(
            f"kind IN ({_quoted(KINDS)}) AND metric IN ({_quoted(METRICS)})"
            f" AND value >= 0 AND value <= {MAX_VALUE}"
            " AND (note IS NULL OR length(btrim(note)) > 0)"
            " AND length(btrim(reason)) > 0",
            name="entry",
        ),
    )
    # The latest entry of each kind and metric is the current one.
    op.create_index(
        "bounty_evaluation_entries_bounty_idx",
        "bounty_evaluation_entries",
        ["bounty_id", "kind", "metric", "entry_id"],
    )
    op.create_table(
        "bounty_evaluation_decisions",
        sa.Column(
            "decision_id", sa.BigInteger(), sa.Identity(always=True), primary_key=True
        ),
        sa.Column("bounty_id", sa.UUID(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("integration_status", sa.Text(), nullable=False),
        sa.Column("reasons", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["bounty_id"], ["bounties.bounty_id"]),
        sa.CheckConstraint(
            f"decision IN ({_quoted(DECISIONS)})"
            f" AND integration_status IN ({_quoted(INTEGRATION_STATUSES)})"
            " AND length(btrim(reasons)) > 0",
            name="decision",
        ),
    )
    # The latest decision is the current one.
    op.create_index(
        "bounty_evaluation_decisions_bounty_idx",
        "bounty_evaluation_decisions",
        ["bounty_id", "decision_id"],
    )
    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION bounty_history_immutable()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION bounty_history_immutable()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Refuse while any evaluation this downgrade would drop is recorded.
    for table in APPEND_ONLY:
        if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar():
            raise RuntimeError("refusing to drop recorded bounty evaluations")
    for table in reversed(APPEND_ONLY):
        op.drop_table(table)
