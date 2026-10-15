"""Reopen source-emission payouts discarded by a non-reproducible ambiguity.

A one-off inconsistent archive read made the receipt reader report a subnet
ownership change, and the collector recorded those payouts terminally without
a receipt. Re-reads of the same finalized blocks show no change, so reopen
them; the resolver re-fetches each receipt and requires a terminal verdict to
reproduce before discarding the payout again.

Revision ID: ac176f7e64fc
Revises: 3f1c9a7d2b6e
Create Date: 2026-10-15
"""

from alembic import op

revision: str = "ac176f7e64fc"
down_revision: str | None = "3f1c9a7d2b6e"
branch_labels: str | None = None
depends_on: str | None = None

# JSON null is not SQL NULL: a receipt-less snapshot stores JSON null.
_RECEIPTLESS = "(proof -> 'receipt' IS NULL OR proof -> 'receipt' = 'null'::jsonb)"
_UNRESOLVED = """NOT EXISTS (
    SELECT 1 FROM source_emission_payout_resolutions r
    WHERE r.netuid = source_emission_payouts.netuid
      AND r.block_hash = source_emission_payouts.block_hash
)"""


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE source_emission_payouts
        SET terminal = false, last_checked_at = NULL
        WHERE terminal
          AND blocked_reason LIKE 'unverifiable_payout:%'
          AND {_RECEIPTLESS}
          AND {_UNRESOLVED}
        """
    )


def downgrade() -> None:
    # Restore the old terminal state for rows the rescan has not settled.
    op.execute(
        f"""
        UPDATE source_emission_payouts
        SET terminal = true
        WHERE NOT terminal
          AND (blocked_reason LIKE 'unverifiable_payout:%'
               OR blocked_reason LIKE 'receipt_retry:%')
          AND {_RECEIPTLESS}
          AND {_UNRESOLVED}
        """
    )
