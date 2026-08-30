"""add idempotent shadow coding revoke capabilities

Revision ID: f9c5b2e8d374
Revises: e8b4a1d7c263
Create Date: 2026-08-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f9c5b2e8d374"
down_revision: str | Sequence[str] | None = "e8b4a1d7c263"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "coding_inference_grants",
        sa.Column("revoke_bearer_digest", sa.Text(), nullable=True),
    )
    op.drop_constraint(
        "coding_inference_grants_crypto_check",
        "coding_inference_grants",
        type_="check",
    )
    op.drop_constraint(
        "coding_inference_grants_state_check",
        "coding_inference_grants",
        type_="check",
    )
    op.create_check_constraint(
        "coding_inference_grants_crypto_check",
        "coding_inference_grants",
        "inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
        "AND (bearer_digest IS NULL OR bearer_digest ~ '^[0-9a-f]{64}$') "
        "AND (revoke_bearer_digest IS NULL OR "
        "revoke_bearer_digest ~ '^[0-9a-f]{64}$') "
        "AND (broker_public_key IS NULL OR "
        "broker_public_key ~ '^[A-Za-z0-9_-]{43}$')",
    )
    op.create_check_constraint(
        "coding_inference_grants_state_check",
        "coding_inference_grants",
        "status IN ('pending', 'active', 'revoked', 'exhausted') "
        "AND ((status = 'pending' AND generation = 0 "
        "AND bearer_digest IS NULL AND revoke_bearer_digest IS NULL "
        "AND broker_public_key IS NULL) "
        "OR (status = 'active' AND generation > 0 "
        "AND bearer_digest IS NOT NULL AND broker_public_key IS NOT NULL) "
        "OR (status IN ('revoked', 'exhausted') "
        "AND bearer_digest IS NULL AND broker_public_key IS NULL)) "
        "AND ((status = 'revoked') = (revoked_at IS NOT NULL))",
    )


def downgrade() -> None:
    op.drop_constraint(
        "coding_inference_grants_state_check",
        "coding_inference_grants",
        type_="check",
    )
    op.drop_constraint(
        "coding_inference_grants_crypto_check",
        "coding_inference_grants",
        type_="check",
    )
    op.create_check_constraint(
        "coding_inference_grants_crypto_check",
        "coding_inference_grants",
        "inference_grant_sha256 ~ '^[0-9a-f]{64}$' "
        "AND (bearer_digest IS NULL OR bearer_digest ~ '^[0-9a-f]{64}$') "
        "AND (broker_public_key IS NULL OR "
        "broker_public_key ~ '^[A-Za-z0-9_-]{43}$')",
    )
    op.create_check_constraint(
        "coding_inference_grants_state_check",
        "coding_inference_grants",
        "status IN ('pending', 'active', 'revoked', 'exhausted') "
        "AND ((status = 'pending' AND generation = 0 "
        "AND bearer_digest IS NULL AND broker_public_key IS NULL) "
        "OR (status = 'active' AND generation > 0 "
        "AND bearer_digest IS NOT NULL AND broker_public_key IS NOT NULL) "
        "OR (status IN ('revoked', 'exhausted') "
        "AND bearer_digest IS NULL AND broker_public_key IS NULL)) "
        "AND ((status = 'revoked') = (revoked_at IS NOT NULL))",
    )
    op.drop_column("coding_inference_grants", "revoke_bearer_digest")
