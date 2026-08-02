"""add owner_attestations: cryptographically-proven same-operator links

A miner who rotates keys currently loses the same-owner copy-screening
exemption, because that exemption keys on the payment-time coldkey. The
principle behind it is owner-based -- copying is only a threat across owners --
but the implementation is key-based, so an honest miner who moved keys gets
copy-flagged against their own earlier work and the only remedy is an operator
believing them in Discord.

This table records the self-serve replacement: two hotkeys declared to be the
same operator, with both endpoints having signed. Each half is proved by either
that hotkey itself or the coldkey bound to it by payment records, and the key
kind is stored per side so a reviewer can grade the evidence. Both signatures
are stored verbatim so the claim is re-verifiable offline by anyone, including
the miner disputing a hold.

The pair is stored in canonical (sorted) order, so an unordered pair has
exactly one row and the partial unique index below constrains the pair itself
rather than one arrangement of it.

Scope, restated here because a schema outlives its PR: this table feeds copy
screening only. It is not referenced by ``emission_owner_key`` in
``ditto/db/queries/scores.py``, which remains the single authority for
one-slot-per-owner emission partitioning.

Revision ID: 7b41d0e29c85
Revises: c8a2f640d31e
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7b41d0e29c85"
down_revision: str | None = "d4b8e6c1a205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "owner_attestations"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("attestation_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("hotkey_lo", sa.Text(), nullable=False),
        sa.Column("hotkey_hi", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("lo_key_kind", sa.Text(), nullable=False),
        sa.Column("lo_signer", sa.Text(), nullable=False),
        sa.Column("lo_signature", sa.Text(), nullable=False),
        sa.Column("hi_key_kind", sa.Text(), nullable=False),
        sa.Column("hi_signer", sa.Text(), nullable=False),
        sa.Column("hi_signature", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("attestation_id", name="owner_attestations_pkey"),
        # The replay guard. A captured attestation cannot be resubmitted.
        sa.UniqueConstraint("nonce", name="owner_attestations_nonce_key"),
        # Enforces canonical ordering in DDL, which is what lets the partial
        # unique index below constrain the unordered pair.
        sa.CheckConstraint(
            "hotkey_lo < hotkey_hi",
            name="owner_attestations_canonical_order",
        ),
        sa.CheckConstraint(
            "lo_key_kind IN ('hotkey', 'coldkey')",
            name="owner_attestations_lo_key_kind_check",
        ),
        sa.CheckConstraint(
            "hi_key_kind IN ('hotkey', 'coldkey')",
            name="owner_attestations_hi_key_kind_check",
        ),
        sa.CheckConstraint(
            "length(lo_signature) = 128",
            name="owner_attestations_lo_signature_check",
        ),
        sa.CheckConstraint(
            "length(hi_signature) = 128",
            name="owner_attestations_hi_signature_check",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by IS NULL)",
            name="owner_attestations_revocation_pair",
        ),
    )
    # At most one *active* link per pair. Partial, so a revoked link can be
    # re-established later with a fresh nonce.
    op.create_index(
        "owner_attestations_active_pair_idx",
        _TABLE,
        ["netuid", "hotkey_lo", "hotkey_hi"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    # Screening-time resolution reads both columns: the link is symmetric, so
    # neither side is privileged and both need an index.
    op.create_index("owner_attestations_lo_idx", _TABLE, ["netuid", "hotkey_lo"])
    op.create_index("owner_attestations_hi_idx", _TABLE, ["netuid", "hotkey_hi"])


def downgrade() -> None:
    op.drop_index("owner_attestations_hi_idx", table_name=_TABLE)
    op.drop_index("owner_attestations_lo_idx", table_name=_TABLE)
    op.drop_index("owner_attestations_active_pair_idx", table_name=_TABLE)
    op.drop_table(_TABLE)
