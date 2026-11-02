"""forward the scorer's own confirmation failure class and status

Revision ID: 8a31c6d4f902
Revises: 5b8e2d4f6a13
Create Date: 2026-10-28

``failure_class`` was built to localize a confirmation failure fleet-wide, and
for reporter-side breaks it does. For scorer-side breaks it does not: the
reporter derives the class from its own exception **type**, every error the
trusted scorer returns arrives as the same type, and so every one of them lands
here as ``dittobench`` at stage ``running_confirmation``. Measured on the
2026-10 lane that is not a corner case but the whole population -- 47 failed
bundles, all identical in this ledger, spanning four validators and several
artifacts, and separable only by reading one line of host logs on whichever
validator claimed the ticket. For a managed or third-party validator nobody can
read that line, so the diagnosis cost ~$14 per attempt in the hope the next one
landed on a host we own.

The scorer already emits a bounded class of its own
(``longmemeval.FailureDiagnostic`` -> ``longmem_<operation>_<kind>``) with an
HTTP status, and the validator already parses both off the protected
scorer-response headers before dropping them into an exception message. This
widens the persisted allowlist to the scorer's thirteen slugs so the reporter
can forward what it already holds, and adds the bounded status beside it.

Still allowlisted, still low-cardinality, still signature-bound (the reporter
signs a v3 fail message when it sends a status), and still not an error-string
channel: an unknown slug is rejected here exactly as before, and a reporter
that cannot classify falls back to its type-derived class.

The values are written out rather than imported from
``ditto_screening_protocol``. A migration is a snapshot of the schema at one
instant, and importing a list that is expected to grow would silently rewrite
this revision's meaning the next time the scorer gains a kind.
``test_failure_class_constraint_matches_the_shared_allowlist`` holds the head
schema and the shared allowlist together instead, which is the invariant that
actually matters and the one a drifted slug breaks -- at the exact moment a
failure is being reported.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8a31c6d4f902"
down_revision: str | Sequence[str] | None = "5b8e2d4f6a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "confirmation_bundle_tickets"
_CLASS_CONSTRAINT = "confirmation_tickets_failure_class_check"
_STATUS_CONSTRAINT = "confirmation_tickets_failure_status_check"

_REPORTER_CLASS_VALUES = (
    "sandbox_oom",
    "lease_revoked",
    "validator_infrastructure",
    "platform_infrastructure",
    "dittobench",
    "platform",
    "validator",
    "evidence_schema",
    "timeout",
    "transport",
    "unclassified",
)
_SCORER_CLASS_VALUES = (
    "longmem_run_http_status",
    "longmem_run_malformed_json",
    "longmem_run_missing_final_text",
    "longmem_run_request",
    "longmem_run_response_read",
    "longmem_run_response_too_large",
    "longmem_run_tool_endpoint",
    "longmem_seed_http_status",
    "longmem_seed_incomplete_ack",
    "longmem_seed_malformed_json",
    "longmem_seed_request",
    "longmem_seed_response_read",
    "longmem_seed_response_too_large",
)

_STATUS_EXPRESSION = (
    "failure_status IS NULL OR "
    "(failure_class IS NOT NULL AND failure_status BETWEEN 0 AND 599)"
)


def _class_expression(values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"failure_class IS NULL OR failure_class IN ({rendered})"


def upgrade() -> None:
    op.drop_constraint(_CLASS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CLASS_CONSTRAINT,
        _TABLE,
        _class_expression((*_REPORTER_CLASS_VALUES, *_SCORER_CLASS_VALUES)),
    )
    op.add_column(_TABLE, sa.Column("failure_status", sa.Integer(), nullable=True))
    op.create_check_constraint(_STATUS_CONSTRAINT, _TABLE, _STATUS_EXPRESSION)


def downgrade() -> None:
    op.drop_constraint(_STATUS_CONSTRAINT, _TABLE, type_="check")
    op.drop_column(_TABLE, "failure_status")

    # Narrowing the allowlist would orphan any ticket already handed back with
    # a scorer class, and a failed ticket is immutable signed history. Clear the
    # forwarded diagnostic back to the type-derived class those reporters would
    # otherwise have sent, rather than stranding the row or failing the
    # downgrade: the hand-back itself, its reason, and its stage are unaffected.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET failure_class = 'dittobench' "  # noqa: S608
            "WHERE failure_class LIKE 'longmem\\_%'"
        )
    )
    op.drop_constraint(_CLASS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CLASS_CONSTRAINT, _TABLE, _class_expression(_REPORTER_CLASS_VALUES)
    )
