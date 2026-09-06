"""Append-only native authoring evidence authority.

Revision ID: a2e815bd034f
Revises: f1d704ac923e
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a2e815bd034f"
down_revision = "f1d704ac923e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coding_hosted_authoring_reservations",
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
            ["coding_hosted_assignments.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "identity_sha256 ~ '^[0-9a-f]{64}$' "
            "AND jsonb_typeof(identity)='object' "
            "AND octet_length(identity::text)<=16384",
            name="hosted_authoring_identity",
        ),
    )
    op.create_table(
        "coding_hosted_authoring_finalizations",
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
            ["coding_hosted_authoring_reservations.evaluation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "probe_sha256 ~ '^[0-9a-f]{64}$'", name="hosted_authoring_probe"
        ),
    )
    op.execute("""
    CREATE FUNCTION guard_hosted_authoring_insert() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE a coding_hosted_assignments%ROWTYPE; i jsonb; s jsonb;
            reserved_at timestamptz;
    BEGIN
      SELECT * INTO a FROM coding_hosted_assignments
        WHERE evaluation_id=NEW.evaluation_id;
      IF TG_TABLE_NAME='coding_hosted_authoring_reservations' THEN
        i:=NEW.identity; s:=i->'source';
        IF a.started_at IS NULL
          OR s->>'evaluation_id' IS DISTINCT FROM a.evaluation_id::text
          OR s->>'attempt_id' IS DISTINCT FROM a.attempt_id::text
          OR s->>'worker_id' IS DISTINCT FROM a.worker_id::text
          OR s->>'assignment_sha256' IS DISTINCT FROM a.assignment_sha256
          OR s->>'artifact_sha256' IS DISTINCT FROM a.artifact_sha256
          OR s->>'profile_capability_id' IS DISTINCT FROM 'hosted-'||a.attempt_id::text
          OR s->>'deadline_unix' IS DISTINCT FROM
            floor(extract(epoch FROM a.expires_at))::bigint::text
          OR i->>'schema' IS DISTINCT FROM 'dittobench-coding-authoring-evidence-v2'
          OR i->'weight_eligible' IS DISTINCT FROM 'false'::jsonb
          OR i->>'publication_deadline_unix' IS DISTINCT FROM
            (floor(extract(epoch FROM a.expires_at))::bigint+86400)::text
          OR NEW.created_at < a.started_at OR NEW.created_at > clock_timestamp()
        THEN RAISE EXCEPTION 'native authoring evidence authority mismatch'; END IF;
      ELSE
        SELECT identity, created_at INTO i, reserved_at
          FROM coding_hosted_authoring_reservations
          WHERE evaluation_id=NEW.evaluation_id;
        IF i IS NULL OR NEW.verified_at < reserved_at
          OR NEW.verified_at > clock_timestamp()
        THEN RAISE EXCEPTION 'native authoring evidence unavailable'; END IF;
      END IF;
      IF extract(epoch FROM clock_timestamp()) >=
        (i->>'publication_deadline_unix')::bigint
      THEN RAISE EXCEPTION 'native authoring publication expired'; END IF;
      RETURN NEW;
    END $$
    """)
    for table in (
        "coding_hosted_authoring_reservations",
        "coding_hosted_authoring_finalizations",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_insert BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_hosted_authoring_insert()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()"
        )


def downgrade() -> None:
    op.drop_table("coding_hosted_authoring_finalizations")
    op.drop_table("coding_hosted_authoring_reservations")
    op.execute("DROP FUNCTION guard_hosted_authoring_insert()")
