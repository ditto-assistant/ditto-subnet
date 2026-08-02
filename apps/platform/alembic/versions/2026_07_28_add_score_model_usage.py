"""record what a scored run actually spent on the language model

Revision ID: a7c41f8b2e93
Revises: 7b41d0e29c85
Create Date: 2026-07-27

Whether a submission used the reader model at all is currently answerable only
by joining ``scores`` to ``inference_grants``. That join is not durable:
``inference_grants`` is a hot, short-retention table -- production holds roughly
three days of rows -- while ``scores`` is permanent. Past the retention horizon
the question becomes unanswerable for any score already written, which is
precisely the wrong property for a number that decides emissions.

So the three counters are denormalized onto the score at submit time, read off
the one grant bound to that ticket's lease.

All three are **nullable with no default and no backfill**. ``NULL`` means *not
measured* -- the inference proxy was disabled for that lease, or the row
predates this migration -- and is deliberately distinguishable from ``0``,
which means *measured, and the model was never called*. Collapsing those two
into a single value would make every historical score look like an abuser.
Nothing in this migration reads or rewrites an existing row.

``scores`` is not one of the hot tables (``inference_requests``,
``inference_grants``, ``validator_tickets``), but ``safe_add_column`` is used
anyway: it is metadata-only for a nullable undefaulted add, so it costs nothing
and keeps lock acquisition bounded by ``lock_timeout`` with retry. No hot table
is touched by this migration at all -- the grant is only ever read.
"""

from collections.abc import Sequence

from ditto.db.migration_lock import safe_add_column, safe_drop_column

revision: str = "a7c41f8b2e93"
down_revision: str | Sequence[str] | None = "7b41d0e29c85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("model_calls", "INTEGER"),
    ("model_prompt_tokens", "BIGINT"),
    ("model_completion_tokens", "BIGINT"),
)


def upgrade() -> None:
    for name, sql_type in _COLUMNS:
        safe_add_column("scores", name, sql_type)


def downgrade() -> None:
    for name, _ in reversed(_COLUMNS):
        safe_drop_column("scores", name)
