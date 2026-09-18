"""Bound complete full-profile receipt audits, including retries.

Revision ID: 9ce14a73b206
Revises: 82fb617ce934
"""

from alembic import op

revision = "9ce14a73b206"
down_revision = "82fb617ce934"
branch_labels = None
depends_on = None


def _replace(maximum: int) -> None:
    name = op.f("ck_private_benchmark_datasets_content_hashes")
    op.drop_constraint(name, "private_benchmark_datasets", type_="check")
    op.create_check_constraint(
        name,
        "private_benchmark_datasets",
        "base_sha256 = encode(sha256(base_bytes), 'hex') AND "
        "dataset_sha256 = encode(sha256(dataset_bytes), 'hex') AND "
        "validation_receipt_sha256 = "
        "encode(sha256(validation_receipt_bytes), 'hex') AND "
        f"octet_length(validation_receipt_bytes) BETWEEN 1 AND {maximum}",
    )


def upgrade() -> None:
    _replace(32 << 20)


def downgrade() -> None:
    # PostgreSQL rejects this transaction if a larger receipt exists; never
    # truncate immutable evidence to make a downgrade succeed.
    _replace(4 << 20)
