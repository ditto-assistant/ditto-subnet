"""store the Kaniko docker-save config digest on the build row

Revision ID: b7e2c91a04d6
Revises: a1c8e4f29b70
Create Date: 2026-08-20

Validators ``docker load`` the Kaniko tar. DittoBench classic matching
requires ``{configDigest}.json``. Pinning the Artifact Registry config
digest after skopeo promote is a different identity, so scoring fails
closed in ~15s. The builder already has the tar; it posts that digest
here and Platform binds it. Nullable: in-flight jobs and historical rows
have no value, and bind fail-closes without one.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e2c91a04d6"
down_revision: str | Sequence[str] | None = "a1c8e4f29b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "submission_image_builds",
        sa.Column("output_image_id", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "submission_image_builds_output_image_id_check",
        "submission_image_builds",
        "output_image_id IS NULL OR output_image_id ~ '^sha256:[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "submission_image_builds_output_image_id_check",
        "submission_image_builds",
        type_="check",
    )
    op.drop_column("submission_image_builds", "output_image_id")
