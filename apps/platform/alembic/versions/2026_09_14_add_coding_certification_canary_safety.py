"""recover claimed coding certification leases and add a strict allowlist

Revision ID: c7a2e5d19b43
Revises: e6f4a9c2d781
Create Date: 2026-09-14

Default-off safety changes for the shadow contract-v1 certification path.

1. Lease lifecycle. A claimed lease whose receipt window has passed may become
   ``expired`` while keeping its ``claimed_at`` audit timestamp, so one
   post-claim failure no longer holds the in-flight unique slot for that exact
   agent, artifact, image, and benchmark forever. A lease whose receipt was
   accepted becomes ``completed``, which is terminal and never expires. An
   allowlist revision may abort an in-flight lease, including a claimed one,
   and records itself in ``aborted_allowlist_revision``.

   Renewal. Issue refuses an identity only while one of its accepted receipts
   (``coding_capability_certifications.expires_at``) is still valid on the
   database clock, not forever. The check reads receipts by identity, so legacy
   receipts need no new linkage: a pre-lease receipt (``lease_id IS NULL``) and
   a receipt whose lease is not ``claimed`` block exactly while they are valid.

   Legacy normalization (deterministic, no row deleted): every lease that
   carries a receipt and was left ``claimed`` (or, after a downgrade of this
   revision, ``expired`` with ``claimed_at``) becomes ``completed``. The upgrade
   logs an audit of the legacy receipt rows it found.

2. ``coding_certification_allowlist_revisions`` is an append-only, strict
   operator setting. No row refuses every certification lease, claim, harness,
   grant, and receipt; only an enabled revision admits its exact tuples. It
   never participates in scoring, weights, or emissions.

3. ``claim_allowlist_revision`` records the revision that admitted each claim.
   The per-identity attempt budget counts only such admitted claims.

The table is small, is not a hot table, and no trigger on it changes.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7a2e5d19b43"
down_revision: str | Sequence[str] | None = "e6f4a9c2d781"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "coding_certification_leases"
_STATUS = "coding_certification_leases_status_check"
_LIFECYCLE = "coding_certification_leases_lifecycle_check"
_CLAIM_ALLOWLIST = "coding_certification_leases_claim_allowlist_check"
_CLAIM_ALLOWLIST_FK = "coding_certification_leases_claim_allowlist_fkey"
_ABORTED_ALLOWLIST_FK = "coding_certification_leases_aborted_allowlist_fkey"

_PRIOR_STATUS = "status IN ('issued', 'claimed', 'aborted', 'expired')"
_STRICT_STATUS = "status IN ('issued', 'claimed', 'completed', 'aborted', 'expired')"
_PRIOR_LIFECYCLE = (
    "(status = 'issued' AND claimed_at IS NULL AND aborted_at IS NULL) "
    "OR (status = 'claimed' AND claimed_at IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at IS NULL) "
    "OR (status = 'aborted' AND aborted_at IS NOT NULL "
    "AND aborted_at >= issued_at AND claimed_at IS NULL) "
    "OR (status = 'expired' AND claimed_at IS NULL AND aborted_at IS NULL)"
)
# Keep byte-identical with ``ditto.db.models.CODING_CERTIFICATION_LEASE_LIFECYCLE``.
_RECOVERABLE_LIFECYCLE = (
    "(status = 'issued' AND claimed_at IS NULL AND aborted_at IS NULL "
    "AND aborted_allowlist_revision IS NULL) "
    "OR (status IN ('claimed', 'completed') AND claimed_at IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at IS NULL AND aborted_allowlist_revision IS NULL) "
    "OR (status = 'aborted' AND aborted_at IS NOT NULL "
    "AND aborted_at >= issued_at AND (claimed_at IS NULL "
    "OR (aborted_allowlist_revision IS NOT NULL "
    "AND claimed_at >= issued_at AND claimed_at < deadline "
    "AND aborted_at >= claimed_at))) "
    "OR (status = 'expired' AND aborted_at IS NULL "
    "AND aborted_allowlist_revision IS NULL "
    "AND (claimed_at IS NULL "
    "OR (claimed_at >= issued_at AND claimed_at < deadline)))"
)


def upgrade() -> None:
    # Both helpers apply the metadata naming convention, so each replacement
    # keeps the exact constraint name the ORM model and later migrations use.
    op.drop_constraint(_LIFECYCLE, _TABLE, type_="check")
    op.drop_constraint(_STATUS, _TABLE, type_="check")

    op.create_table(
        "coding_certification_allowlist_revisions",
        sa.Column("revision", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("parent_revision", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("entries", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checksum", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "parent_revision >= 0 AND parent_revision < revision",
            name="coding_certification_allowlist_parent_check",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(entries) = 'array' "
            "AND jsonb_array_length(entries) <= 16 "
            "AND enabled = (jsonb_array_length(entries) > 0)",
            name="coding_certification_allowlist_entries_check",
        ),
        sa.CheckConstraint(
            "checksum ~ '^[0-9a-f]{64}$'",
            name="coding_certification_allowlist_checksum_check",
        ),
        sa.CheckConstraint(
            "length(trim(reason)) >= 8",
            name="coding_certification_allowlist_reason_check",
        ),
        sa.CheckConstraint(
            "length(trim(actor)) BETWEEN 1 AND 120",
            name="coding_certification_allowlist_actor_check",
        ),
        sa.PrimaryKeyConstraint("revision"),
        sa.UniqueConstraint(
            "parent_revision",
            name="coding_certification_allowlist_parent_key",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION guard_coding_certification_allowlist_append_only()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'coding certification allowlist revisions are append-only'
                USING ERRCODE = '23514',
                      CONSTRAINT = 'coding_certification_allowlist_append_only_guard';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER coding_certification_allowlist_revisions_append_only_guard
        BEFORE UPDATE OR DELETE ON coding_certification_allowlist_revisions
        FOR EACH ROW
        EXECUTE FUNCTION guard_coding_certification_allowlist_append_only()
        """
    )

    op.add_column(
        _TABLE, sa.Column("claim_allowlist_revision", sa.Integer(), nullable=True)
    )
    op.add_column(
        _TABLE, sa.Column("aborted_allowlist_revision", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        _CLAIM_ALLOWLIST_FK,
        _TABLE,
        "coding_certification_allowlist_revisions",
        ["claim_allowlist_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        _ABORTED_ALLOWLIST_FK,
        _TABLE,
        "coding_certification_allowlist_revisions",
        ["aborted_allowlist_revision"],
        ["revision"],
        ondelete="RESTRICT",
    )

    _audit_legacy_receipts()
    # A lease whose receipt Platform already accepted is terminal. Before this
    # revision such a lease simply stayed ``claimed``; a downgrade of this
    # revision may also have left one ``expired`` with its ``claimed_at``. An
    # unclaimed lease cannot carry an accepted receipt, and is left untouched.
    op.execute(
        """
        UPDATE coding_certification_leases AS lease
        SET status = 'completed'
        WHERE lease.status IN ('claimed', 'expired')
          AND lease.claimed_at IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM coding_capability_certifications AS receipt
              WHERE receipt.lease_id = lease.lease_id
          )
        """
    )

    op.create_check_constraint(_STATUS, _TABLE, _STRICT_STATUS)
    op.create_check_constraint(_LIFECYCLE, _TABLE, _RECOVERABLE_LIFECYCLE)
    op.create_check_constraint(
        _CLAIM_ALLOWLIST,
        _TABLE,
        "claim_allowlist_revision IS NULL "
        "OR (claim_allowlist_revision > 0 AND claimed_at IS NOT NULL)",
    )


def _audit_legacy_receipts() -> None:
    """Log, before normalizing, the receipt rows the renewal rule reads.

    Read-only and deterministic apart from ``valid_now``, which uses the
    database clock exactly as issue does. Identifiers and digests are never
    logged, only counts.
    """

    row = (
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT
                    count(*) FILTER (WHERE receipt.lease_id IS NULL)
                        AS without_lease,
                    count(*) FILTER (
                        WHERE receipt.lease_id IS NULL
                          AND receipt.expires_at > clock_timestamp()
                    ) AS without_lease_valid_now,
                    count(*) FILTER (
                        WHERE lease.status = 'claimed'
                    ) AS claimed_lease_to_complete,
                    count(*) FILTER (
                        WHERE receipt.lease_id IS NOT NULL
                          AND lease.status <> 'claimed'
                    ) AS lease_not_claimed,
                    count(*) FILTER (
                        WHERE receipt.expires_at > clock_timestamp()
                    ) AS valid_now
                FROM coding_capability_certifications AS receipt
                LEFT JOIN coding_certification_leases AS lease
                    ON lease.lease_id = receipt.lease_id
                """
            )
        )
        .one()
    )
    logging.getLogger("alembic.runtime.migration").info(
        "coding certification receipt audit: %d without a lease (%d still "
        "valid), %d claimed leases normalized to completed, %d on a lease that "
        "is not claimed, %d still blocking renewal",
        row.without_lease,
        row.without_lease_valid_now,
        row.claimed_lease_to_complete,
        row.lease_not_claimed,
        row.valid_now,
    )


def downgrade() -> None:
    """Restore the prior schema. This is not a safe rollback for a live canary.

    The prior code has no allowlist, so a downgrade reopens certification lease
    issue to every qualified agent and validator, and it drops the allowlist
    revision history and each lease's admitting and aborting revision.
    """

    op.drop_constraint(_CLAIM_ALLOWLIST, _TABLE, type_="check")
    op.drop_constraint(_LIFECYCLE, _TABLE, type_="check")
    op.drop_constraint(_STATUS, _TABLE, type_="check")
    # The prior schema represented a receipted lease as ``claimed``, but its
    # in-flight unique index admits one ``issued`` or ``claimed`` lease per
    # identity, and renewal can leave several completed leases, or a completed
    # lease beside a new in-flight one. Restore ``claimed`` for the newest
    # completed lease of an identity with nothing in flight; every other
    # completed lease becomes ``expired`` keeping ``claimed_at`` (the restored
    # lifecycle CHECK is NOT VALID). The prior code still refuses the identity
    # from its receipt rows, and a later upgrade maps both back to ``completed``.
    op.execute(
        """
        UPDATE coding_certification_leases
        SET status = 'claimed'
        WHERE lease_id IN (
            SELECT DISTINCT ON (
                lease.agent_id, lease.artifact_sha256,
                lease.screened_image_sha256, lease.bench_version,
                lease.coding_contract_version
            ) lease.lease_id
            FROM coding_certification_leases AS lease
            WHERE lease.status = 'completed'
              AND NOT EXISTS (
                  SELECT 1 FROM coding_certification_leases AS other
                  WHERE other.agent_id = lease.agent_id
                    AND other.artifact_sha256 = lease.artifact_sha256
                    AND other.screened_image_sha256 = lease.screened_image_sha256
                    AND other.bench_version = lease.bench_version
                    AND other.coding_contract_version
                        = lease.coding_contract_version
                    AND other.status IN ('issued', 'claimed')
              )
            ORDER BY lease.agent_id, lease.artifact_sha256,
                lease.screened_image_sha256, lease.bench_version,
                lease.coding_contract_version,
                lease.claimed_at DESC, lease.lease_id DESC
        )
        """
    )
    op.execute(
        "UPDATE coding_certification_leases SET status = 'expired' "
        "WHERE status = 'completed'"
    )
    # The prior schema cannot represent a claimed lease aborted by an allowlist
    # revision. Map it to the one terminal no-receipt state a claimed lease can
    # re-enter this revision with (``expired``, keeping ``claimed_at``), so a
    # later upgrade validates its lifecycle CHECK over every row.
    op.execute(
        "UPDATE coding_certification_leases "
        "SET status = 'expired', aborted_at = NULL "
        "WHERE status = 'aborted' AND claimed_at IS NOT NULL"
    )
    op.drop_constraint(_ABORTED_ALLOWLIST_FK, _TABLE, type_="foreignkey")
    op.drop_constraint(_CLAIM_ALLOWLIST_FK, _TABLE, type_="foreignkey")
    op.drop_column(_TABLE, "aborted_allowlist_revision")
    op.drop_column(_TABLE, "claim_allowlist_revision")
    op.execute(
        "DROP TRIGGER IF EXISTS "
        "coding_certification_allowlist_revisions_append_only_guard "
        "ON coding_certification_allowlist_revisions"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS guard_coding_certification_allowlist_append_only()"
    )
    op.drop_table("coding_certification_allowlist_revisions")
    op.create_check_constraint(_STATUS, _TABLE, _PRIOR_STATUS)
    # Claimed-then-expired and allowlist-aborted claimed rows are audit history
    # and are never rewritten or deleted. The restored stricter CHECK is NOT
    # VALID so it binds new writes without refusing the downgrade over rows the
    # newer code legitimately made.
    op.create_check_constraint(
        _LIFECYCLE,
        _TABLE,
        _PRIOR_LIFECYCLE,
        postgresql_not_valid=True,
    )
