"""Backroom airdrop runs (SN-490): run ledger and per-recipient settlement.

Two new tables and nothing else; no existing table is touched. A run is
named forever by its idempotency key. Each recipient row carries its own
idempotency key and a status that only moves forward, so a retried execute
resumes the run and never sends a recipient twice. Triggers keep the terms
immutable and refuse deletes and truncates, as the bounty ledgers do.

No executor that can move funds ships with this revision: every run it can
hold today is a rehearsal.
"""

import sqlalchemy as sa

from alembic import op

revision = "d307b3894c95"
down_revision = "b44f233dc32f"
branch_labels = None
depends_on = None

_TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "airdrop_runs",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("asset", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("executor", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("recipient_count", sa.Integer(), nullable=False),
        sa.Column("total_amount", sa.BigInteger(), nullable=False),
        sa.Column("created_at", _TS, server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("run_id", name="pk_airdrop_runs"),
        sa.UniqueConstraint("idempotency_key", name="uq_airdrop_runs_idempotency_key"),
        sa.CheckConstraint(
            "asset IN ('alpha', 'ditto_credits')", name="ck_airdrop_runs_asset"
        ),
        sa.CheckConstraint(
            "mode IN ('rehearsal', 'live')", name="ck_airdrop_runs_mode"
        ),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="ck_airdrop_runs_digest"),
        sa.CheckConstraint("length(trim(reason)) >= 8", name="ck_airdrop_runs_reason"),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 320", name="ck_airdrop_runs_actor"
        ),
        sa.CheckConstraint(
            "recipient_count BETWEEN 1 AND 1000",
            name="ck_airdrop_runs_recipient_count",
        ),
        sa.CheckConstraint("total_amount > 0", name="ck_airdrop_runs_total_amount"),
    )
    op.create_index("airdrop_runs_created_idx", "airdrop_runs", ["created_at"])

    op.create_table(
        "airdrop_recipients",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("recipient_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("payee", sa.Text(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column(
            "status", sa.Text(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("receipt", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempted_at", _TS, nullable=True),
        sa.Column("settled_at", _TS, nullable=True),
        sa.PrimaryKeyConstraint("run_id", "position", name="pk_airdrop_recipients"),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["airdrop_runs.run_id"],
            name="fk_airdrop_recipients_run_id_airdrop_runs",
        ),
        sa.UniqueConstraint(
            "recipient_key", name="uq_airdrop_recipients_recipient_key"
        ),
        sa.UniqueConstraint(
            "run_id", "kind", "address", name="uq_airdrop_recipients_run_address"
        ),
        sa.CheckConstraint(
            "kind IN ('wallet', 'account')", name="ck_airdrop_recipients_kind"
        ),
        sa.CheckConstraint(
            "recipient_key ~ '^[0-9a-f]{64}$'",
            name="ck_airdrop_recipients_recipient_key",
        ),
        sa.CheckConstraint("amount > 0", name="ck_airdrop_recipients_amount"),
        sa.CheckConstraint("position >= 0", name="ck_airdrop_recipients_position"),
        sa.CheckConstraint(
            "status IN ('pending', 'dispatching', 'settled', 'failed', 'uncertain')",
            name="ck_airdrop_recipients_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (attempted_at IS NULL)",
            name="ck_airdrop_recipients_attempted",
        ),
    )
    op.create_index(
        "airdrop_recipients_payee_idx", "airdrop_recipients", ["payee", "status"]
    )

    # A run's terms are fixed at insert; only completed_at is set, once.
    op.execute("""CREATE FUNCTION airdrop_run_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'UPDATE' THEN
            RAISE EXCEPTION 'airdrop runs are append-only';
          END IF;
          IF to_jsonb(NEW) - 'completed_at'
             IS DISTINCT FROM to_jsonb(OLD) - 'completed_at'
          THEN
            RAISE EXCEPTION 'airdrop run terms are immutable';
          END IF;
          IF OLD.completed_at IS NOT NULL
             AND NEW.completed_at IS DISTINCT FROM OLD.completed_at THEN
            RAISE EXCEPTION 'airdrop run completion is final';
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER airdrop_runs_guard BEFORE UPDATE OR DELETE ON airdrop_runs "
        "FOR EACH ROW EXECUTE FUNCTION airdrop_run_guard()"
    )
    # A recipient's terms are fixed; its status only moves forward, and a row
    # that left 'pending' can never return to it.
    op.execute("""CREATE FUNCTION airdrop_recipient_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'UPDATE' THEN
            RAISE EXCEPTION 'airdrop recipients are append-only';
          END IF;
          IF to_jsonb(NEW) - 'status' - 'receipt' - 'error'
               - 'attempted_at' - 'settled_at'
             IS DISTINCT FROM
             to_jsonb(OLD) - 'status' - 'receipt' - 'error'
               - 'attempted_at' - 'settled_at'
          THEN
            RAISE EXCEPTION 'airdrop recipient terms are immutable';
          END IF;
          IF OLD.status IN ('settled', 'failed') THEN
            RAISE EXCEPTION 'airdrop recipient % is final', OLD.status;
          END IF;
          IF OLD.attempted_at IS NOT NULL
             AND NEW.attempted_at IS DISTINCT FROM OLD.attempted_at THEN
            RAISE EXCEPTION 'airdrop recipient attempt time is final';
          END IF;
          IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
               (OLD.status = 'pending' AND NEW.status = 'dispatching')
            OR (OLD.status = 'dispatching'
                AND NEW.status IN ('settled', 'failed', 'uncertain'))
            OR (OLD.status = 'uncertain' AND NEW.status IN ('settled', 'failed'))
          ) THEN
            RAISE EXCEPTION 'airdrop recipient cannot move from % to %',
              OLD.status, NEW.status;
          END IF;
          RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER airdrop_recipients_guard BEFORE UPDATE OR DELETE "
        "ON airdrop_recipients FOR EACH ROW EXECUTE FUNCTION airdrop_recipient_guard()"
    )
    op.execute("""CREATE FUNCTION airdrop_no_truncate()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'airdrop history cannot be truncated'; END $$""")
    for table in ("airdrop_runs", "airdrop_recipients"):
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION airdrop_no_truncate()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT EXISTS (SELECT 1 FROM airdrop_runs)")).scalar():
        raise RuntimeError("refusing to drop recorded airdrop history")
    op.drop_table("airdrop_recipients")
    op.drop_table("airdrop_runs")
    op.execute("DROP FUNCTION airdrop_no_truncate()")
    op.execute("DROP FUNCTION airdrop_recipient_guard()")
    op.execute("DROP FUNCTION airdrop_run_guard()")
