"""allow gcp as the stored Cloud Run screening provider

Revision ID: a1c8e4f29b70
Revises: e4a9c7b1d083
Create Date: 2026-08-20

Platform-attested Kaniko, smoke, and L1 one-shots can now run on Cloud Run as
the GCP fallback when Targon is at capacity or never leaves provisioning.
The operator provider lists stay ``targon`` / ``gcp``; this only widens the
job-row check so a Cloud Run execution can be recorded as ``gcp``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1c8e4f29b70"
down_revision: str | Sequence[str] | None = "e4a9c7b1d083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECKS = (
    (
        "submission_image_builds",
        "submission_image_builds_provider_check",
        "provider IS NULL OR provider IN ('targon', 'gcp')",
        "provider IS NULL OR provider = 'targon'",
    ),
    (
        "submission_source_reviews",
        "submission_source_reviews_provider_check",
        "provider IS NULL OR provider IN ('targon', 'gcp')",
        "provider IS NULL OR provider = 'targon'",
    ),
)


def upgrade() -> None:
    for table, name, widened, _narrow in _CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, widened)


def downgrade() -> None:
    for table, name, _widened, narrow in _CHECKS:
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, narrow)
