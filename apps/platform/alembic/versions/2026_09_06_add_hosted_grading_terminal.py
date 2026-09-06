"""Native grading claims, sealed terminal evidence and result delivery.

Revision ID: b3f926ce145a
Revises: a2e815bd034f
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b3f926ce145a"
down_revision = "a2e815bd034f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coding_hosted_grading_claims",
        sa.Column("evaluation_id", sa.UUID(), primary_key=True),
        sa.Column("claim_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("binding", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["coding_hosted_authoring_finalizations.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(binding)='object' AND octet_length(binding::text)<=16384",
            name="hosted_grading_binding",
        ),
    )
    op.create_table(
        "coding_hosted_terminal_reservations",
        sa.Column("evaluation_id", sa.UUID(), primary_key=True),
        sa.Column("identity_sha256", sa.Text(), nullable=False, unique=True),
        sa.Column("identity", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["coding_hosted_grading_claims.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "identity_sha256 ~ '^[0-9a-f]{64}$' AND "
            "jsonb_typeof(identity)='object' AND "
            "octet_length(identity::text)<=16384",
            name="hosted_terminal_identity",
        ),
    )
    op.create_table(
        "coding_hosted_terminal_finalizations",
        sa.Column("evaluation_id", sa.UUID(), primary_key=True),
        sa.Column("probe_sha256", sa.Text(), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["coding_hosted_terminal_reservations.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "probe_sha256 ~ '^[0-9a-f]{64}$'", name="hosted_terminal_probe"
        ),
    )
    op.create_table(
        "coding_hosted_result_deliveries",
        sa.Column("result_sha256", sa.Text(), primary_key=True),
        sa.Column("evaluation_id", sa.UUID(), nullable=False),
        sa.Column("validator_hotkey", sa.Text(), nullable=False),
        sa.Column("body", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["evaluation_id"],
            ["coding_hosted_terminal_finalizations.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "result_sha256 ~ '^[0-9a-f]{64}$' AND "
            "jsonb_typeof(body)='object' AND "
            "octet_length(body::text)<=8192",
            name="hosted_delivery_body",
        ),
    )
    op.create_table(
        "coding_hosted_result_acknowledgements",
        sa.Column("result_sha256", sa.Text(), primary_key=True),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["result_sha256"],
            ["coding_hosted_result_deliveries.result_sha256"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "request_sha256 ~ '^[0-9a-f]{64}$'", name="hosted_ack_request"
        ),
    )
    op.execute("""
    CREATE FUNCTION guard_hosted_grading_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE a coding_hosted_assignments%ROWTYPE; t coding_hosted_private_tasks%ROWTYPE;
            c coding_hosted_grading_claims%ROWTYPE; i jsonb;
    BEGIN
      SELECT * INTO a FROM coding_hosted_assignments
          WHERE evaluation_id=NEW.evaluation_id;
      IF TG_TABLE_NAME='coding_hosted_grading_claims' THEN
        SELECT * INTO t FROM coding_hosted_private_tasks
          WHERE evaluation_id=NEW.evaluation_id;
        i:=NEW.binding;
        IF a.started_at IS NULL
          OR a.expires_at<=clock_timestamp()
          OR t.frozen_at IS NULL
          OR t.closed_at IS NOT NULL
          OR i->>'frozen_patch_sha256' IS DISTINCT FROM t.frozen_patch_sha256

          OR i->>'grading_profile_sha256'
          IS DISTINCT FROM a.authority->>'grading_profile_sha256'
          OR i->'source'->>'worker_id' IS DISTINCT FROM a.worker_id::text
          OR i->'source'->>'attempt_id' IS DISTINCT FROM a.attempt_id::text
          OR i->'source'->>'assignment_sha256' IS DISTINCT FROM a.assignment_sha256
        THEN RAISE EXCEPTION 'hosted grading claim mismatch'; END IF;
      ELSIF TG_TABLE_NAME='coding_hosted_terminal_reservations' THEN
        SELECT * INTO c FROM coding_hosted_grading_claims
          WHERE evaluation_id=NEW.evaluation_id;
        i:=NEW.identity;
        IF i->>'claim_id' IS DISTINCT FROM c.claim_id::text

          OR i->>'authoring_evidence_sha256'
          IS DISTINCT FROM c.binding->>'authoring_evidence_sha256'

          OR i->>'grading_profile_sha256'
          IS DISTINCT FROM c.binding->>'grading_profile_sha256'
          OR i->'weight_eligible' IS DISTINCT FROM 'false'::jsonb

          OR i->>'outcome' NOT IN
            ('completed','candidate_failure','infrastructure_failure','integrity_failure')

          OR (i->>'outcome' IN ('completed','candidate_failure')
          AND clock_timestamp()>=a.expires_at)
        THEN RAISE EXCEPTION 'hosted terminal identity mismatch'; END IF;
      END IF;
      IF TG_TABLE_NAME!='coding_hosted_grading_claims'
          AND clock_timestamp()>=a.expires_at+interval '24 hours'
      THEN RAISE EXCEPTION 'hosted terminal publication expired'; END IF;
      RETURN NEW;
    END $$
    """)
    op.execute(
        "CREATE TRIGGER coding_hosted_grading_claims_insert "
        "BEFORE INSERT ON coding_hosted_grading_claims "
        "FOR EACH ROW EXECUTE FUNCTION guard_hosted_grading_insert()"
    )
    op.execute(
        "CREATE TRIGGER coding_hosted_terminal_reservations_insert "
        "BEFORE INSERT ON coding_hosted_terminal_reservations "
        "FOR EACH ROW EXECUTE FUNCTION guard_hosted_grading_insert()"
    )
    op.execute(
        "CREATE TRIGGER coding_hosted_terminal_finalizations_insert "
        "BEFORE INSERT ON coding_hosted_terminal_finalizations "
        "FOR EACH ROW EXECUTE FUNCTION guard_hosted_grading_insert()"
    )
    op.execute(
        "CREATE TRIGGER coding_hosted_grading_claims_immutable "
        "BEFORE UPDATE OR DELETE ON coding_hosted_grading_claims "
        "FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()"
    )
    op.execute(
        "CREATE TRIGGER coding_hosted_terminal_reservations_immutable "
        "BEFORE UPDATE OR DELETE ON coding_hosted_terminal_reservations "
        "FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()"
    )
    op.execute(
        "CREATE TRIGGER coding_hosted_terminal_finalizations_immutable "
        "BEFORE UPDATE OR DELETE ON coding_hosted_terminal_finalizations "
        "FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()"
    )
    op.execute(
        "CREATE TRIGGER coding_hosted_result_deliveries_immutable "
        "BEFORE UPDATE OR DELETE ON coding_hosted_result_deliveries "
        "FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()"
    )
    op.execute(
        "CREATE TRIGGER coding_hosted_result_acknowledgements_immutable "
        "BEFORE UPDATE OR DELETE ON coding_hosted_result_acknowledgements "
        "FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()"
    )


def downgrade() -> None:
    op.drop_table("coding_hosted_result_acknowledgements")
    op.drop_table("coding_hosted_result_deliveries")
    op.drop_table("coding_hosted_terminal_finalizations")
    op.drop_table("coding_hosted_terminal_reservations")
    op.drop_table("coding_hosted_grading_claims")
    op.execute("DROP FUNCTION guard_hosted_grading_insert()")
