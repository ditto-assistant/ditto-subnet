"""Append-only private operator funding reconciliation; no public receipt mutation."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "8f6a2c4e9b71"
down_revision = "3d8f1a6c2e57"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "treasury_funding_reconciliations",
        sa.Column("purchase_id", sa.Text(), primary_key=True),
        sa.Column("payment_reference", sa.Text(), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.execute("""CREATE FUNCTION reject_treasury_funding_mutation() RETURNS trigger
      LANGUAGE plpgsql AS $$ BEGIN
        RAISE EXCEPTION 'operator funding reconciliation history is append only';
      END $$""")
    op.execute("""CREATE TRIGGER treasury_funding_immutable
      BEFORE UPDATE OR DELETE ON treasury_funding_reconciliations
      FOR EACH ROW EXECUTE FUNCTION reject_treasury_funding_mutation()""")

    op.execute(
        "CREATE TRIGGER treasury_funding_no_truncate BEFORE TRUNCATE "
        "ON treasury_funding_reconciliations FOR EACH STATEMENT "
        "EXECUTE FUNCTION reject_treasury_funding_mutation()"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT 1 FROM treasury_funding_reconciliations)")
    ):
        raise RuntimeError("Funding audit history prevents downgrade")
    op.execute(
        "DROP TRIGGER treasury_funding_immutable ON treasury_funding_reconciliations"
    )
    op.execute(
        "DROP TRIGGER treasury_funding_no_truncate ON treasury_funding_reconciliations"
    )
    op.execute("DROP FUNCTION reject_treasury_funding_mutation()")
    op.drop_table("treasury_funding_reconciliations")
