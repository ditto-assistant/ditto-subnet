"""record why a confirmation provider call failed

Revision ID: d09a6cba3154
Revises: 3f7a1c9e5b26
Create Date: 2026-10-30

A failed confirmation request row carried only ``status = 'failed'``. The relay
held the upstream HTTP status, its attempt count, whether the call timed out,
and whether the failure provably left no provider receipt -- and wrote all of
it to a host-local log line and nothing else. Measured 2026-10-29: with the
judge route fixed (#3269), the next blocker was the reader lane failing about
once per 120 calls (11 of 1347 over seven days), each one recorded as
"failed, no provider, no cost" and nothing more. Whether those are transient
5xx, timeouts or resets is exactly what decides if a retry is safe and how
it must back off, and it could not be answered from anywhere but the hosts.

Four nullable columns, null on completed calls and on every row before this
revision. ``failure_kind`` is a closed vocabulary set by the relay; nothing
here is derived from an error string.

The CHECKs are added ``NOT VALID`` and validated in an autocommit block, so
the only exclusive lock is the metadata-only ALTER; the validating scan holds
SHARE UPDATE EXCLUSIVE and does not block settle writes.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d09a6cba3154"
down_revision: str | Sequence[str] | None = "3f7a1c9e5b26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "confirmation_inference_requests"
_KIND = "confirmation_inference_requests_failure_kind_check"
_DETAIL = "confirmation_inference_requests_failure_detail_check"
_KINDS = (
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


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("failure_kind", sa.Text(), nullable=True))
    op.add_column(
        _TABLE, sa.Column("upstream_http_status", sa.Integer(), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("upstream_attempts", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("receipt_free", sa.Boolean(), nullable=True))
    kinds = ", ".join(f"'{kind}'" for kind in _KINDS)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_KIND} CHECK "  # noqa: S608
        f"(failure_kind IS NULL OR failure_kind IN ({kinds})) NOT VALID"
    )
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_DETAIL} CHECK ("  # noqa: S608
        "(upstream_http_status IS NULL "
        "OR upstream_http_status BETWEEN 100 AND 599) "
        "AND (upstream_attempts IS NULL OR upstream_attempts BETWEEN 0 AND 16)) "
        "NOT VALID"
    )
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_KIND}")
        op.execute(f"ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_DETAIL}")


def downgrade() -> None:
    # Raw SQL with the literal names, matching how upgrade added them: the
    # metadata naming convention rewrites a name passed to op.drop_constraint
    # into ck_<table>_<truncated>_<hash>, which is not the name these were
    # created under, so the helper would look for a constraint that is not there.
    op.execute(
        f"ALTER TABLE {_TABLE} "
        f"DROP CONSTRAINT IF EXISTS {_DETAIL}, "
        f"DROP CONSTRAINT IF EXISTS {_KIND}"
    )
    op.drop_column(_TABLE, "receipt_free")
    op.drop_column(_TABLE, "upstream_attempts")
    op.drop_column(_TABLE, "upstream_http_status")
    op.drop_column(_TABLE, "failure_kind")
