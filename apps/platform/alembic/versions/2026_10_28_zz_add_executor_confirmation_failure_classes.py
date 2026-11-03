"""admit the scorer's executor-stage confirmation failure classes

Revision ID: 6b4f8a2c91d3
Revises: 5e2b9d7c4a18
Create Date: 2026-10-28

``8a31c6d4f902`` widened this allowlist to the scorer's thirteen harness-boundary
classes so a confirmation failure could name the boundary that broke instead of
collapsing to ``dittobench``. Measured in production the same day, on the first
attempt carrying that change (bundle ``9d764326``, an operator retest of
``67e55f57`` on a validator verified to be serving the passthrough): the failure
still reported ``dittobench`` with no status.

That was not the passthrough failing, it was the passthrough answering. The
scorer only emits a harness class for an error matching
``longmemeval.FailureDiagnostic``; for anything else it returns
``unclassified``, and the handler suppresses the diagnostic headers entirely in
that case. So the absence of a class proved, by elimination, that no seed call,
no ``/run`` call and no case failure was involved -- the break is in the
executor's own scaffolding -- and simultaneously guaranteed that the one
remaining fact, WHICH stage, could never leave the host.

The stage was already computed (``confirmationExecutionStage``) and already
written to the scorer's local log one line below the suppressed header. These
eleven classes forward it. Same discipline as the harness classes: a closed
vocabulary the scorer's own code defines, no free text, no status (0 only --
there is no HTTP exchange behind a stage failure), and an unknown stage still
degrades to ``unclassified`` rather than being passed through.

Written out rather than imported from ``ditto_screening_protocol`` for the
reason the previous revision gives: a migration is a snapshot, and importing a
growing tuple would silently rewrite this revision's meaning. The pairing is
held by ``test_failure_class_constraint_matches_the_shared_allowlist`` instead,
and on the Go side by a test that parses every
``wrapConfirmationExecutionFailure`` call site in the binary.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6b4f8a2c91d3"
down_revision: str | Sequence[str] | None = "5e2b9d7c4a18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TABLE = "confirmation_bundle_tickets"
_CLASS_CONSTRAINT = "confirmation_tickets_failure_class_check"

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
_HARNESS_CLASS_VALUES = (
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
_EXECUTOR_CLASS_VALUES = (
    "longmem_exec_ablation_dataset",
    "longmem_exec_broker_session",
    "longmem_exec_dimension_execution",
    "longmem_exec_embedding_phase",
    "longmem_exec_execution",
    "longmem_exec_harness_health",
    "longmem_exec_installation_validation",
    "longmem_exec_longmem_dataset",
    "longmem_exec_provider_capabilities",
    "longmem_exec_provider_session",
    "longmem_exec_runtime_acquire",
    "longmem_exec_runtime_close",
    "longmem_exec_runtime_material",
    "longmem_exec_runtime_validation",
    "longmem_exec_sandbox_availability",
    "longmem_exec_sandbox_start",
    "longmem_exec_sandbox_stop",
    "longmem_exec_screened_image",
    "longmem_exec_source_validation",
)


def _class_expression(values: Sequence[str]) -> str:
    rendered = ", ".join(f"'{value}'" for value in values)
    return f"failure_class IS NULL OR failure_class IN ({rendered})"


def upgrade() -> None:
    op.drop_constraint(_CLASS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CLASS_CONSTRAINT,
        _TABLE,
        _class_expression(
            (*_REPORTER_CLASS_VALUES, *_HARNESS_CLASS_VALUES, *_EXECUTOR_CLASS_VALUES)
        ),
    )


def downgrade() -> None:
    # A failed ticket is immutable signed history, so narrowing the allowlist
    # must not strand rows that already carry an executor class. Fold them back
    # to the coarse class the reporter would otherwise have sent; the hand-back,
    # its reason and its stage are untouched.
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET failure_class = 'dittobench' "  # noqa: S608
            "WHERE failure_class LIKE 'longmem\\_exec\\_%'"
        )
    )
    op.drop_constraint(_CLASS_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(
        _CLASS_CONSTRAINT,
        _TABLE,
        _class_expression((*_REPORTER_CLASS_VALUES, *_HARNESS_CLASS_VALUES)),
    )
