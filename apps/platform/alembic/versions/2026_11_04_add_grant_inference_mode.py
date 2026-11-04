"""add the inference mode to inference grants (chat-disabled grants)

Revision ID: 6b3f9d2e8a17
Revises: d9e291eb6ec3
Create Date: 2026-11-04

A grant's ``inference_mode`` says which hosted lanes it may use. ``chat`` (the
default, and every grant minted before this revision) is unchanged: chat,
embeddings and, when the grant carries a decision pin, the decision lane.
``no_chat`` is the no-inference competition mode (docs/no-inference-mode.md):
the relay refuses every chat call on the grant as ``model_not_permitted``
(4107) while embeddings and the decision lane stay open. A ``no_chat`` grant
must carry a decision pin, since without one it could only embed.

No producer stamps ``no_chat`` yet. The column defaults to ``chat``, so every
grant minted after this revision is byte-identical to one minted before it
until the mode's mint path lands.

``inference_grants`` is hot (#481). One ``ALTER TABLE`` adds the column as a
non-volatile ``NOT NULL DEFAULT`` (metadata only on PostgreSQL 11+) plus a
CHECK ``NOT VALID``; the constraint scan runs afterwards under ``SHARE UPDATE
EXCLUSIVE``, which does not block writers. Re-runnable from any point.
"""

from collections.abc import Sequence

from alembic import op
from ditto.db.migration_lock import run_with_retry

revision: str = "6b3f9d2e8a17"
down_revision: str | Sequence[str] | None = "d9e291eb6ec3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK = "ck_inference_grants_inference_grants_inference_mode"
_CHECK_SQL = (
    "inference_mode IN ('chat', 'no_chat') "
    "AND (inference_mode = 'chat' OR decision_model IS NOT NULL)"
)


def upgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        run_with_retry(
            bind,
            [
                "ALTER TABLE inference_grants "
                "ADD COLUMN IF NOT EXISTS inference_mode TEXT NOT NULL DEFAULT 'chat', "
                f"DROP CONSTRAINT IF EXISTS {_CHECK}, "
                f"ADD CONSTRAINT {_CHECK} CHECK ({_CHECK_SQL}) NOT VALID"
            ],
            "add inference_grants.inference_mode",
        )
        run_with_retry(
            bind,
            [f"ALTER TABLE inference_grants VALIDATE CONSTRAINT {_CHECK}"],
            "validate inference_grants.inference_mode",
        )


def downgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        run_with_retry(
            bind,
            [
                "ALTER TABLE inference_grants "
                f"DROP CONSTRAINT IF EXISTS {_CHECK}, "
                "DROP COLUMN IF EXISTS inference_mode"
            ],
            "drop inference_grants.inference_mode",
        )
