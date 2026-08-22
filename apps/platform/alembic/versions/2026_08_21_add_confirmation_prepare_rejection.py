"""persist the allowlisted prepare-report rejection on the confirmation ticket

Revision ID: c8f4e1a92b70
Revises: b7e2c91a04d6
Create Date: 2026-08-21

Four LongMem canaries completed reader, judge, and ablation execute, then died
at ``platform`` / ``finalizing`` because ``/prepare-report`` returned 409.
The HTTP detail was the exact ConfirmationWireError or ConfirmationEvidenceError
string, but it never left the validator process: journald, W&B, and Backroom
only saw the later fail-job class. This pair of nullable columns is that 409,
closed over an allowlist so it cannot become an exception-string channel.

Written inside the prepare transaction before the 409 is returned; surviving
fail-job, expiry, and success-clears so a later report cannot be read as if
prepare never ran. Null is correct for every historical row and for tickets
that never hit convert/rebuild. The pair is always both-null or both-set.

Does not change score, retry ownership, or settlement. Prepare remains
non-authoritative; the ticket stays issued until fail/expire/complete.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f4e1a92b70"
down_revision: str | Sequence[str] | None = "b7e2c91a04d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREPARE_REJECTION_VALUES = (
    "'go_evidence_digest_mismatch', 'go_evidence_fields_drifted', "
    "'unsupported_ablation_status', 'unsupported_ablation_contract', "
    "'ablation_profile_drift', 'ablation_accounting', "
    "'ablation_digest_mismatch', 'longmem_profile_drift', "
    "'longmem_accounting', 'longmem_digest_mismatch', "
    "'longmem_latency_drift', 'unsupported_bench_version', "
    "'confirmation_wire', 'confirmation_evidence', 'unclassified'"
)


def upgrade() -> None:
    op.add_column(
        "confirmation_bundle_tickets",
        sa.Column("prepare_rejection", sa.Text(), nullable=True),
    )
    op.add_column(
        "confirmation_bundle_tickets",
        sa.Column("prepare_rejected_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "confirmation_tickets_prepare_rejection_pair_check",
        "confirmation_bundle_tickets",
        "(prepare_rejection IS NULL) = (prepare_rejected_at IS NULL)",
    )
    op.create_check_constraint(
        "confirmation_tickets_prepare_rejection_check",
        "confirmation_bundle_tickets",
        "prepare_rejection IS NULL OR prepare_rejection IN "
        f"({_PREPARE_REJECTION_VALUES})",
    )


def downgrade() -> None:
    op.drop_constraint(
        "confirmation_tickets_prepare_rejection_check",
        "confirmation_bundle_tickets",
        type_="check",
    )
    op.drop_constraint(
        "confirmation_tickets_prepare_rejection_pair_check",
        "confirmation_bundle_tickets",
        type_="check",
    )
    op.drop_column("confirmation_bundle_tickets", "prepare_rejected_at")
    op.drop_column("confirmation_bundle_tickets", "prepare_rejection")
