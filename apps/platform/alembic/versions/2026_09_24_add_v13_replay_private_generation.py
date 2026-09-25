"""Bind private generation to independent replay images.

Revision ID: cfa2be70013d
Revises: 0e7a1c9b4d62
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "cfa2be70013d"
down_revision: str | Sequence[str] | None = "0e7a1c9b4d62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v13_replay_private_generation_groups",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("replay_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_artifact_sha256", sa.Text(), nullable=False),
        sa.Column("target_image_sha256", sa.Text(), nullable=False),
        sa.Column("control_agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("control_attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("control_artifact_sha256", sa.Text(), nullable=False),
        sa.Column("control_image_sha256", sa.Text(), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("profile_sha256", sa.Text(), nullable=False),
        sa.Column("target_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("control_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["replay_id"],
            ["screening_verification_replays.replay_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_agent_id"], ["agents.agent_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["control_agent_id"], ["agents.agent_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["control_attempt_id"],
            ["screening_attempts.attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["v13_known_benign_control_approvals.approval_id"],
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("replay_id", name="v13rpg_replay_key"),
        sa.CheckConstraint(
            "target_agent_id <> control_agent_id", name="v13rpg_distinct_agents"
        ),
        *(
            sa.CheckConstraint(f"length({name}) = 64", name=f"v13rpg_{name}_check")
            for name in (
                "target_artifact_sha256",
                "target_image_sha256",
                "control_artifact_sha256",
                "control_image_sha256",
                "approval_receipt_sha256",
                "profile_sha256",
                "target_receipt_sha256",
                "control_receipt_sha256",
            )
        ),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 120", name="v13rpg_actor"),
    )
    op.create_table(
        "v13_replay_group_package_registrations",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("role", sa.Text(), primary_key=True),
        sa.Column("generation_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("pair_inventory_sha256", sa.Text(), nullable=False),
        sa.Column("registrar_actor", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["v13_replay_private_generation_groups.group_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("role IN ('target', 'known_benign')", name="v13rgp_role"),
        *(
            sa.CheckConstraint(f"length({name}) = 64", name=f"v13rgp_{name}")
            for name in (
                "generation_receipt_sha256",
                "manifest_sha256",
                "pair_inventory_sha256",
            )
        ),
        sa.CheckConstraint(
            "length(registrar_actor) BETWEEN 1 AND 120", name="v13rgp_actor"
        ),
    )
    for table in (
        "v13_replay_private_generation_groups",
        "v13_replay_group_package_registrations",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_v13_private_generation_mutation()"
        )


def downgrade() -> None:
    for table in (
        "v13_replay_group_package_registrations",
        "v13_replay_private_generation_groups",
    ):
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.drop_table("v13_replay_group_package_registrations")
    op.drop_table("v13_replay_private_generation_groups")
