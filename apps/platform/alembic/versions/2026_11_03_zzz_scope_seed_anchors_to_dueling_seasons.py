"""scope confirmation seed anchors to dueling seasons

Revision ID: 3c8f1d2a9b64
Revises: fd7cc6236807
Create Date: 2026-11-03

From bench v16 a dueling season rotation re-anchors the champion's
confirmation family: one anchor per ``(champion, bench_version, season)``
instead of per ``(champion, bench_version)``. ``season_key`` is the season id
at v16+ and the nil UUID everywhere else, so every existing row keeps its
identity and every v13-v15 family is unchanged. ``confirmation_seed_anchors``
holds one row per reign (tens of rows) and is off the request hot path, so the
column add and primary-key swap are plain DDL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3c8f1d2a9b64"
down_revision: str | Sequence[str] | None = "fd7cc6236807"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "confirmation_seed_anchors"
NIL = "'00000000-0000-0000-0000-000000000000'::uuid"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "season_key",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text(NIL),
        ),
    )
    op.create_check_constraint(
        "confirmation_seed_anchors_season_floor",
        TABLE,
        f"season_key = {NIL} OR bench_version >= 16",
    )
    op.drop_constraint("confirmation_seed_anchors_pkey", TABLE, type_="primary")
    op.create_primary_key(
        "confirmation_seed_anchors_pkey",
        TABLE,
        ["champion_agent_id", "bench_version", "season_key"],
    )


def downgrade() -> None:
    # A v16+ reign anchored once per season collapses back to one row per
    # reign: keep the oldest anchor of each reign.
    op.execute(
        f"""
        DELETE FROM {TABLE} AS a
        USING {TABLE} AS b
        WHERE a.champion_agent_id = b.champion_agent_id
          AND a.bench_version = b.bench_version
          AND (a.anchor_block, a.season_key) > (b.anchor_block, b.season_key)
        """
    )
    op.drop_constraint("confirmation_seed_anchors_pkey", TABLE, type_="primary")
    op.create_primary_key(
        "confirmation_seed_anchors_pkey", TABLE, ["champion_agent_id", "bench_version"]
    )
    op.drop_constraint("confirmation_seed_anchors_season_floor", TABLE, type_="check")
    op.drop_column(TABLE, "season_key")
