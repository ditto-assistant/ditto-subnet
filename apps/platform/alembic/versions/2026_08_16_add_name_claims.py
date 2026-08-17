"""add name_claims: signed handle reservation with miner endorsements

A miner who has been using a public handle (``Jupiter``) currently has no
way to stop another hotkey uploading as ``Jupiter-ditto-v10`` short of a
Discord appeal. These tables record the self-serve replacement: the
claimant signs a reservation over the normalized name stem, and other
miners who already have a scored payment-owner family endorse it. An
upheld stem is reserved to the claimant's owner family for new uploads.

Signatures are stored verbatim so the claim is re-verifiable offline.
The tables are not referenced by ``emission_owner_key``: this is a
public-name control, not an emission-slot control.

Revision ID: a8c1e4f29b70
Revises: c4d9e2f18a67
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8c1e4f29b70"
down_revision: str | Sequence[str] | None = "c4d9e2f18a67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIMS = "name_claims"
_ENDORSEMENTS = "name_claim_endorsements"


def upgrade() -> None:
    op.create_table(
        _CLAIMS,
        sa.Column("claim_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("netuid", sa.Integer(), nullable=False),
        sa.Column("name_stem", sa.Text(), nullable=False),
        sa.Column("claimant_hotkey", sa.Text(), nullable=False),
        sa.Column("claimant_key_kind", sa.Text(), nullable=False),
        sa.Column("claimant_signer", sa.Text(), nullable=False),
        sa.Column("claimant_signature", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("upheld_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("withdrawn_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("withdrawn_signer", sa.Text(), nullable=True),
        sa.Column("withdrawn_signature", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("claim_id", name="name_claims_pkey"),
        sa.UniqueConstraint("nonce", name="name_claims_nonce_key"),
        sa.CheckConstraint(
            "status IN ('pending', 'upheld', 'withdrawn')",
            name="name_claims_status_check",
        ),
        sa.CheckConstraint(
            "claimant_key_kind IN ('hotkey', 'coldkey')",
            name="name_claims_key_kind_check",
        ),
        sa.CheckConstraint(
            "length(claimant_signature) = 128",
            name="name_claims_signature_check",
        ),
        sa.CheckConstraint(
            "length(name_stem) BETWEEN 3 AND 64",
            name="name_claims_stem_length_check",
        ),
        sa.CheckConstraint(
            "name_stem ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="name_claims_stem_charset_check",
        ),
        sa.CheckConstraint(
            "(status = 'upheld') = (upheld_at IS NOT NULL)",
            name="name_claims_upheld_pair",
        ),
        sa.CheckConstraint(
            "(status = 'withdrawn') = (withdrawn_at IS NOT NULL)",
            name="name_claims_withdrawn_pair",
        ),
    )
    # At most one live (pending or upheld) reservation per stem. A withdrawn
    # claim frees the stem so a later rightful owner can take it.
    op.create_index(
        "name_claims_active_stem_idx",
        _CLAIMS,
        ["netuid", "name_stem"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'upheld')"),
    )
    op.create_index("name_claims_status_idx", _CLAIMS, ["netuid", "status"])
    op.create_index("name_claims_claimant_idx", _CLAIMS, ["netuid", "claimant_hotkey"])

    op.create_table(
        _ENDORSEMENTS,
        sa.Column("endorsement_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("claim_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("endorser_hotkey", sa.Text(), nullable=False),
        sa.Column("endorser_owner_root", sa.Text(), nullable=False),
        sa.Column("endorser_key_kind", sa.Text(), nullable=False),
        sa.Column("endorser_signer", sa.Text(), nullable=False),
        sa.Column("endorser_signature", sa.Text(), nullable=False),
        sa.Column("nonce", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("endorsement_id", name="name_claim_endorsements_pkey"),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["name_claims.claim_id"],
            ondelete="CASCADE",
            name="name_claim_endorsements_claim_id_fkey",
        ),
        sa.UniqueConstraint("nonce", name="name_claim_endorsements_nonce_key"),
        sa.UniqueConstraint(
            "claim_id",
            "endorser_owner_root",
            name="name_claim_endorsements_owner_key",
        ),
        sa.CheckConstraint(
            "endorser_key_kind IN ('hotkey', 'coldkey')",
            name="name_claim_endorsements_key_kind_check",
        ),
        sa.CheckConstraint(
            "length(endorser_signature) = 128",
            name="name_claim_endorsements_signature_check",
        ),
    )
    op.create_index(
        "name_claim_endorsements_claim_idx",
        _ENDORSEMENTS,
        ["claim_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("name_claim_endorsements_claim_idx", table_name=_ENDORSEMENTS)
    op.drop_table(_ENDORSEMENTS)
    op.drop_index("name_claims_claimant_idx", table_name=_CLAIMS)
    op.drop_index("name_claims_status_idx", table_name=_CLAIMS)
    op.drop_index("name_claims_active_stem_idx", table_name=_CLAIMS)
    op.drop_table(_CLAIMS)
