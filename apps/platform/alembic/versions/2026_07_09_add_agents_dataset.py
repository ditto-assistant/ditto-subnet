"""add agents.dataset_seed / dataset_sha256 / dataset_run_size

Revision ID: c8f2d6a01b93
Revises: b7e1c4a92f30
Create Date: 2026-07-09 13:00:00.000000

The per-submission dataset the platform generates at job-ready
(``uploaded -> evaluating``, see
:func:`ditto.api_server.endpoints.screener.report_screen_result`). The platform
draws a fresh unpredictable seed, calls the private ditto-data-pipeline generate
service, and stores the seed + the returned DatasetArtifact SHA-256 + the
run_size on the agent. Every k=3 validator ticket for that agent carries these
(``JobResponse``), so all three validators score the IDENTICAL dataset — the
median-of-3 is over one dataset — and the scoring API can regenerate it from the
seed and reject a mismatch (tamper-evidence).

- ``agents.dataset_seed`` — ``BIGINT``, nullable (null until job-ready).
- ``agents.dataset_sha256`` — hex ``TEXT`` (SHA-256), nullable.
- ``agents.dataset_run_size`` — ``TEXT`` (small|medium|full), nullable.

No index: read by agent_id only (the ticket-issue + score paths already fetch the
agent row).
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8f2d6a01b93"
down_revision: str | Sequence[str] | None = "b7e1c4a92f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the three nullable per-submission dataset columns to ``agents``."""
    op.execute("ALTER TABLE agents ADD COLUMN dataset_seed BIGINT")
    op.execute("ALTER TABLE agents ADD COLUMN dataset_sha256 TEXT")
    op.execute("ALTER TABLE agents ADD COLUMN dataset_run_size TEXT")


def downgrade() -> None:
    """Drop the per-submission dataset columns."""
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS dataset_run_size")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS dataset_sha256")
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS dataset_seed")
