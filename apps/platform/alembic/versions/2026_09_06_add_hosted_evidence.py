"""Append-only native inference evidence reservation and readback finalization.

Revision ID: f1d704ac923e
Revises: e0c6f34b18d2
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f1d704ac923e"
down_revision = "e0c6f34b18d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coding_hosted_evidence_reservations",
        sa.Column("request_id", sa.UUID(), primary_key=True),
        sa.Column("reservation_id", sa.UUID(), nullable=False, unique=True),
        sa.Column("identity_sha256", sa.Text(), nullable=False, unique=True),
        sa.Column("identity", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["coding_hosted_inference_requests.request_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "identity_sha256 ~ '^[0-9a-f]{64}$' AND "
            "jsonb_typeof(identity)='object' AND octet_length(identity::text)<=16384",
            name="hosted_evidence_identity",
        ),
    )
    op.create_table(
        "coding_hosted_evidence_finalizations",
        sa.Column("reservation_id", sa.UUID(), primary_key=True),
        sa.Column("probe_sha256", sa.Text(), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["coding_hosted_evidence_reservations.reservation_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "probe_sha256 ~ '^[0-9a-f]{64}$'", name="hosted_evidence_probe"
        ),
    )
    op.execute("""
    CREATE FUNCTION guard_hosted_evidence_insert() RETURNS trigger
    LANGUAGE plpgsql AS $$
    DECLARE r coding_hosted_inference_requests%ROWTYPE;
            g coding_hosted_inference_grants%ROWTYPE;
            i jsonb;
            k text;
            reserved_at timestamptz;
    BEGIN
      IF TG_TABLE_NAME='coding_hosted_evidence_reservations' THEN
        SELECT * INTO r FROM coding_hosted_inference_requests
          WHERE request_id=NEW.request_id;
        SELECT * INTO g FROM coding_hosted_inference_grants WHERE grant_id=r.grant_id;
        i := NEW.identity;
        IF r.state IS DISTINCT FROM 'settled'
          OR i->>'schema' IS DISTINCT FROM
            'dittobench-coding-hosted-inference-evidence-v2'
          OR i->'weight_eligible' IS DISTINCT FROM 'false'::jsonb
          OR i->>'request_id' IS DISTINCT FROM NEW.request_id::text
          OR i->>'reservation_id' IS DISTINCT FROM NEW.reservation_id::text
          OR i->>'grant_id' IS DISTINCT FROM g.grant_id::text
          OR i->>'evaluation_id' IS DISTINCT FROM g.evaluation_id::text
          OR i->>'attempt_id' IS DISTINCT FROM g.attempt_id::text
          OR i->>'worker_id' IS DISTINCT FROM g.worker_id::text
          OR i->>'assignment_sha256' IS DISTINCT FROM g.assignment_sha256
          OR i->>'policy_sha256' IS DISTINCT FROM g.policy_sha256
          OR i->>'settlement_sha256' IS DISTINCT FROM r.settlement_sha256
          OR g.policy->>'runtime_profile_sha256' IS NULL
          OR i->>'runtime_profile_sha256' IS DISTINCT FROM
            g.policy->>'runtime_profile_sha256'
          OR i->>'publication_deadline_unix' IS DISTINCT FROM
            (floor(extract(epoch FROM r.finalized_at))::bigint+86400)::text
          OR clock_timestamp() >= r.finalized_at + interval '24 hours'
          OR NEW.created_at < r.finalized_at
          OR NEW.created_at > clock_timestamp()
        THEN RAISE EXCEPTION 'native evidence authority mismatch'; END IF;
        FOREACH k IN ARRAY ARRAY['plaintext_sha256','ciphertext_sha256',
          'envelope_sha256','wrapping_key_sha256','storage_domain_sha256'] LOOP
          IF jsonb_typeof(i->k) IS DISTINCT FROM 'string'
            OR NOT (i->>k ~ '^[0-9a-f]{64}$')
          THEN RAISE EXCEPTION 'native evidence digest is invalid'; END IF;
        END LOOP;
        IF jsonb_typeof(i->'plaintext_size') IS DISTINCT FROM 'number'
          OR jsonb_typeof(i->'ciphertext_size') IS DISTINCT FROM 'number'
          OR (i->>'plaintext_size')::bigint NOT BETWEEN 1 AND 25165824
          OR (i->>'ciphertext_size')::bigint - (i->>'plaintext_size')::bigint
            NOT BETWEEN 17 AND 2048
        THEN RAISE EXCEPTION 'native evidence size is invalid'; END IF;
      ELSE
        SELECT identity, created_at INTO i, reserved_at
          FROM coding_hosted_evidence_reservations
          WHERE reservation_id=NEW.reservation_id;
        IF i IS NULL OR extract(epoch FROM clock_timestamp()) >=
          (i->>'publication_deadline_unix')::bigint
          OR NEW.verified_at < reserved_at OR NEW.verified_at > clock_timestamp()
        THEN RAISE EXCEPTION 'native evidence publication expired'; END IF;
      END IF;
      RETURN NEW;
    END $$
    """)
    for table in (
        "coding_hosted_evidence_reservations",
        "coding_hosted_evidence_finalizations",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_insert BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_hosted_evidence_insert()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_coding_catalog_append_only()"
        )


def downgrade() -> None:
    op.drop_table("coding_hosted_evidence_finalizations")
    op.drop_table("coding_hosted_evidence_reservations")
    op.execute("DROP FUNCTION guard_hosted_evidence_insert()")
