"""record OpenRouter's in-body confirmation failures as their own kind

Revision ID: 18127d681783
Revises: d09a6cba3154
Create Date: 2026-10-31

OpenRouter answers 200 as soon as a provider accepts a request, before any
token, and a status is final once sent; a failure after that point arrives as a
200 whose body is only an error object. The confirmation relay had no check for
it and recorded it as ``identity_mismatch`` (the envelope has no model
field), which is how bundle ``7fa29ad3`` read on 2026-10-30.

Adds ``upstream_error_envelope`` to the closed ``failure_kind`` set and an
``upstream_error_code`` column for the envelope's own ``error.code``, so a
502 inside a 200 is distinguishable from a 429 inside a 200. Same NOT VALID
then VALIDATE pattern as ``d09a6cba3154``, dropped and re-added by literal
name because that is how they were created.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "18127d681783"
down_revision: str | Sequence[str] | None = "d09a6cba3154"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "confirmation_inference_requests"
_KIND = "confirmation_inference_requests_failure_kind_check"
_CODE = "confirmation_inference_requests_error_code_check"
_PREVIOUS_KINDS = (
    "identity_mismatch",
    "receipt_invalid",
    "response_invalid",
    "response_too_large",
    "settle_rejected",
    "transport_error",
    "transport_timeout",
    "upstream_backpressure",
    "upstream_status",
)
_KINDS = (*_PREVIOUS_KINDS, "upstream_error_envelope")


def _kind_check(kinds: Sequence[str]) -> str:
    rendered = ", ".join(f"'{kind}'" for kind in kinds)
    return f"failure_kind IS NULL OR failure_kind IN ({rendered})"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("upstream_error_code", sa.Integer(), nullable=True))
    op.execute(
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_KIND}, "  # noqa: S608
        f"ADD CONSTRAINT {_KIND} CHECK ({_kind_check(_KINDS)}) NOT VALID, "
        f"ADD CONSTRAINT {_CODE} CHECK "
        "(upstream_error_code IS NULL OR upstream_error_code BETWEEN 400 AND 599) "
        "NOT VALID"
    )
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_KIND}")
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_CODE}")


def downgrade() -> None:
    # Rows classified with the new kind fold back to what the relay recorded
    # before it could tell them apart, so the narrower CHECK still holds.
    op.execute(
        f"UPDATE {_TABLE} SET failure_kind = 'identity_mismatch' "  # noqa: S608
        "WHERE failure_kind = 'upstream_error_envelope'"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CODE}, "  # noqa: S608
        f"DROP CONSTRAINT IF EXISTS {_KIND}, "
        f"ADD CONSTRAINT {_KIND} CHECK ({_kind_check(_PREVIOUS_KINDS)})"
    )
    op.drop_column(_TABLE, "upstream_error_code")
