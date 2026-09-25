"""Register sealed V13 packages per immutable generation group and role.

Revision ID: 1bc9d8a6207e
Revises: 4ac1e3d7098b

No row is inserted by this migration. Legacy attempt-keyed package rows do
not become trusted matched-control registrations.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "1bc9d8a6207e"
down_revision: str | Sequence[str] | None = "4ac1e3d7098b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "v13_group_package_registrations",
        sa.Column("group_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("role", sa.Text(), primary_key=True),
        sa.Column("generation_receipt_sha256", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("pair_inventory_sha256", sa.Text(), nullable=False),
        sa.Column("registrar_actor", sa.Text(), nullable=False),
        sa.Column("registered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["v13_private_generation_groups.group_id"],
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "role IN ('target', 'known_benign')", name="v13gpr_role_check"
        ),
        *(
            sa.CheckConstraint(f"length({name}) = 64", name=f"v13gpr_{name}_check")
            for name in (
                "generation_receipt_sha256",
                "manifest_sha256",
                "pair_inventory_sha256",
            )
        ),
        sa.CheckConstraint(
            "length(registrar_actor) BETWEEN 1 AND 120", name="v13gpr_actor_check"
        ),
    )
    op.execute(
        "CREATE TRIGGER v13_group_package_registrations_immutable "
        "BEFORE UPDATE OR DELETE ON v13_group_package_registrations "
        "FOR EACH ROW EXECUTE FUNCTION reject_v13_private_generation_mutation()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER v13_group_package_registrations_immutable "
        "ON v13_group_package_registrations"
    )
    op.drop_table("v13_group_package_registrations")
