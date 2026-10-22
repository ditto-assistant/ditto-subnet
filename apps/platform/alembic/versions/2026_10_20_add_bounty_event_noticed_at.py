"""Record when a deadline transition was noticed, not only the deadline.

A missed deadline is stamped at the deadline it passed, so every replica and
retry records the same event. When no request or sweep ran for a while after
the deadline, the event is only written later, and until then the claimant
has no event to appeal. ``noticed_at`` keeps when it was written, and the
appeal window runs from the later of the two.
"""

import sqlalchemy as sa

from alembic import op

revision = "6a35116ca9ae"
down_revision = "5de80b078e1c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bounty_events",
        sa.Column("noticed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_bounty_events_noticed_after_recorded"),
        "bounty_events",
        "noticed_at IS NULL OR noticed_at >= recorded_at",
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Refuse while any appeal window depends on a recorded notice time.
    if bind.execute(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM bounty_events WHERE noticed_at IS NOT NULL)"
        )
    ).scalar():
        raise RuntimeError("refusing to drop recorded bounty event notice times")
    op.drop_constraint(
        op.f("ck_bounty_events_noticed_after_recorded"), "bounty_events", type_="check"
    )
    op.drop_column("bounty_events", "noticed_at")
