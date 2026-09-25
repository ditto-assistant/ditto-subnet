"""Persist digest-only V13 private package registrations.

Revision ID: c7e49af025ab
Revises: 8a4d27c0f639
Create Date: 2026-09-23

Registrations are unverified prerequisites, never policy passes or CLEAR.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7e49af025ab"
down_revision: str | Sequence[str] | None = "8a4d27c0f639"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "screening_private_package_registrations",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_sha256", sa.Text(), nullable=False),
        sa.Column("image_sha256", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("pair_inventory_sha256", sa.Text(), nullable=False),
        sa.Column("clean_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clean_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("clean_artifact_sha256", sa.Text(), nullable=False),
        sa.Column("clean_image_sha256", sa.Text(), nullable=False),
        sa.Column("runner_hotkey", sa.Text(), nullable=False),
        sa.Column("registrar_actor", sa.Text(), nullable=False),
        sa.Column(
            "registered_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.agent_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["screening_attempts.attempt_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clean_agent_id"], ["agents.agent_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["clean_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="CASCADE",
        ),
        *(
            sa.CheckConstraint(f"length({name}) = 64", name=f"sppr_{name}_check")
            for name in (
                "artifact_sha256",
                "image_sha256",
                "profile_sha256",
                "manifest_sha256",
                "pair_inventory_sha256",
                "clean_artifact_sha256",
                "clean_image_sha256",
            )
        ),
        sa.CheckConstraint(
            "length(runner_hotkey) BETWEEN 1 AND 120", name="sppr_runner_check"
        ),
        sa.CheckConstraint(
            "length(registrar_actor) BETWEEN 1 AND 120", name="sppr_actor_check"
        ),
    )


def downgrade() -> None:
    op.drop_table("screening_private_package_registrations")
