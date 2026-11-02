"""Backfill the operator ruling code on the cited manual rejection.

Revision ID: 9e4c7a1b6d20
Revises: a40f7d9c621e
Create Date: 2026-09-25

The policy-v13 manual rejection of agent ``bfe3276e`` recorded the operator's
ruling on the quarantine but left the screening lead the screener opened with --
``behavioral-oracle-passed`` -- on the agent row, so the miner-facing
``screening_reason`` / ``screening_reason_code`` pair presented a CLEAR-side
screening code as the terminal decision. Separating the two vocabularies fixes
that for rows written from now on; this guarded repair is only for the
historical row whose artifact, quarantine, and resolution are pinned below.

Every guard is part of the identity. The row is untouched unless the immutable
artifact SHA-256, the ``rejected`` status, the inherited code, and a quarantine
that agent owns, resolved ``reject``, carrying that same lead all still hold --
so a submission that has since been re-screened, re-scored, or otherwise moved
on is left alone rather than relabelled on stale evidence.

The quarantine's own ``reason_code`` and the append-only
``screening_review_events`` ledger keep the screening lead verbatim and are not
restated here: that code is screening-origin provenance and stays readable as
the lead the operator ruled on. Only the agent row is repaired, because only
the agent row is what the miner-facing pair reads.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9e4c7a1b6d20"
down_revision: str | None = "a40f7d9c621e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# agent_id, immutable artifact SHA-256, quarantine_id that records the ruling
_TARGET: tuple[str, str, str] = (
    "bfe3276e-0454-4b6a-a90f-17f2bb63d144",
    "03bd670787ff1816be8de940868df0669fd9f86ce4380292418ce5956a752ad1",
    "c7e4edef-7c23-4ef4-9095-4781f882dc32",
)

# The screening-origin lead the screener opened the quarantine with.
_STALE_CODE = "behavioral-oracle-passed"
# The operator's ruling, matching ``resolution_reason_code("reject")``.
_RULING_CODE = "operator-rejected-quarantine"


def upgrade() -> None:
    agent_id, sha256, quarantine_id = _TARGET
    op.get_bind().execute(
        sa.text(
            """
            UPDATE agents AS agent
            SET screening_reason_code = :ruling_code
            WHERE agent.agent_id = CAST(:agent_id AS uuid)
              AND agent.sha256 = :sha256
              AND agent.status::text = 'rejected'
              AND agent.screening_reason_code = :stale_code
              AND EXISTS (
                  SELECT 1
                  FROM screening_quarantines AS quarantine
                  WHERE quarantine.quarantine_id = CAST(:quarantine_id AS uuid)
                    AND quarantine.agent_id = agent.agent_id
                    AND quarantine.reason_code = :stale_code
                    AND quarantine.status = 'resolved'
                    AND quarantine.resolution = 'reject'
              )
            """
        ),
        {
            "agent_id": agent_id,
            "sha256": sha256,
            "quarantine_id": quarantine_id,
            "stale_code": _STALE_CODE,
            "ruling_code": _RULING_CODE,
        },
    )


def downgrade() -> None:
    # The operator ruling is accurate public metadata. Reinstating the screening
    # lead as though it were the decision on rollback would be the regression
    # this repair exists to undo, so retain it.
    pass
